// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";

import {QuiverToken} from "../QuiverToken.sol";
import {VentureFeeHook} from "./VentureFeeHook.sol";
import {FounderVesting} from "./FounderVesting.sol";
import {VestingDeployer} from "./VestingDeployer.sol";
import {VentureTokenDeployer} from "./VentureTokenDeployer.sol";

interface IWETH9V {
    function deposit() external payable;
}

interface ISwapRouterV3V {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

/// @title VentureFactory
/// @notice Startup-funding launchpad for the Robinhood chain: a founder
///         launches a coin that represents their venture, the public funds it
///         on a linear bonding curve in plain ETH, and hitting the target
///         graduates the coin into a factory-locked Uniswap V4 pool governed
///         by the founder's own fee policy (VentureFeeHook): separate buy and
///         sell taxes (0-4% each) split across dev wallet / holder dividends /
///         auto-liquidity / a market-making bid wall, plus the protocol fee
///         on every trade.
///
///         Raise mechanics, designed against the documented failure modes of
///         open bonding curves (sniper cohorts, order-splitting, dead curves):
///
///           - Integral pricing: buys are priced by the closed-form integral of
///             the linear curve, so splitting an order across transactions
///             yields exactly the same tokens as one buy. There is nothing to
///             game with ordering.
///           - Per-wallet spend cap (founder-set, default 2% of target).
///           - Hard graduation, inside the buy that fills the target (or
///             sells out the curve), so a funded raise never sits frozen
///             waiting on a separate call. The founder takes their declared
///             cut of the raise (max 30%); the remainder becomes pool liquidity.
///           - All-or-nothing: if the deadline passes below target, the raise
///             aborts and every buyer refunds their full spend against
///             returning their tokens. The founder's vested allocation burns.
///           - Founder skin-in-the-game: the founder allocation (max 15% of
///             supply) sits in a linear vesting escrow that only starts at
///             graduation.
///
///         Compliance note: tokens launched here are open ERC-20s, matching
///         the chain's native stock-token model (compliance at the primary
///         market, not per transfer). A transfer-validation module
///         (ERC-7943-style isTransferAllowed + identity registry) would attach
///         inside QuiverToken._update / at buy() if a gated mode is added.
contract VentureFactory is Ownable, ReentrancyGuard, IUnlockCallback {
    using SafeERC20 for IERC20;
    using PoolIdLibrary for PoolKey;

    uint256 public constant TOTAL_SUPPLY = 1_000_000_000 ether;
    uint256 public constant TOTAL_SUPPLY_WHOLE = 1_000_000_000;
    uint256 public constant CURVE_SUPPLY_WHOLE = 600_000_000; // 60% sold on the curve
    /// @notice Whole-supply FDV at the first curve buy. Coupled to
    ///         `minTargetWei`: the cheapest raise that can exist is the whole
    ///         curve supply bought at this start price
    ///         (START * CURVE_SUPPLY/TOTAL_SUPPLY / ethUsd), because the curve
    ///         only ever rises. At $750 that is 0.241 ETH at $1,865/ETH, which
    ///         leaves a 0.5 ETH raise roughly doubling the price from the first
    ///         backer to graduation. A higher start valuation would push the
    ///         cheapest possible raise above the floor and make small raises
    ///         unlaunchable.
    uint256 public constant START_MCAP_USD_8 = 750 * 1e8;
    uint256 public constant MAX_TARGET_WEI = 1_000_000 ether;
    uint64 public constant MIN_RAISE_SECS = 1 days;
    uint64 public constant MAX_RAISE_SECS = 14 days;
    uint32 public constant MIN_VESTING_SECS = 90 days;
    uint32 public constant MAX_VESTING_SECS = 730 days;
    uint16 public constant MAX_FOUNDER_RAISE_BPS = 3_000; // <= 30% of the raise
    uint16 public constant MAX_FOUNDER_SUPPLY_BPS = 1_500; // <= 15% of supply
    uint16 public constant MAX_CURVE_FEE_BPS = 300; // ceiling on both curve fees
    uint64 public constant MIN_SWEEP_DELAY = 180 days;
    /// @notice For this long after launch every wallet, in either mode, may
    ///         put at most 1/LAUNCH_CAP_DIV of the target into the curve. The
    ///         cheapest tokens are the first ones, and without a limit a bot in
    ///         the launch block, or the creator's second wallet, could take most
    ///         of them before a person sees the coin. It does not stop a bot
    ///         with many wallets; it makes each one cost a separate funding.
    uint64 public constant LAUNCH_WINDOW_SECS = 60;
    uint256 internal constant LAUNCH_CAP_DIV = 100;
    int24 public constant TICK_SPACING = 60;
    uint24 public constant LP_FEE = 0;
    uint16 internal constant BPS = 10_000;

    IPoolManager public immutable poolManager;
    VentureFeeHook public immutable hook;
    IWETH9V public immutable weth;
    ISwapRouterV3V public immutable v3Router;
    VestingDeployer public immutable vestingDeployer;
    VentureTokenDeployer public immutable tokenDeployer;
    address public immutable protocolAdmin;

    /// @notice Smallest raise the platform will finish, in wei. A floor on both
    ///         modes: a founder's own target in Guaranteed, and the graduation
    ///         trigger in Open. Immutable rather than admin-settable — it is a
    ///         business floor, and nothing about it should move under a live
    ///         market. Testnets deploy it small so a raise can be driven to
    ///         graduation for real; mainnet ships 0.5 ether.
    uint256 public immutable minTargetWei;

    /// @notice Accepted range for the ETH/USD price a launcher passes in, 8dp.
    ///         The price sizes the curve's start (`START_MCAP_USD_8` at the
    ///         first buy), and it is caller-supplied because a critical state
    ///         transition must not hang on an oracle. Unbounded, a launcher
    ///         could pass an absurd price, open their own curve near zero and
    ///         buy the cheap end first; in Open mode, with no per-wallet cap,
    ///         that is most of the supply. The band caps how far the start can
    ///         be pushed off the honest one. Immutable: testnets deploy it wide
    ///         (their fallback price is scaled up so faucet wallets can
    ///         graduate a raise), mainnet ships a band around the live price.
    uint64 public immutable minEthUsd8;
    uint64 public immutable maxEthUsd8;

    /// @notice Protocol fee on curve buys, taken off the incoming value before
    ///         the curve is quoted, so `spentWei` records net escrow.
    uint16 public immutable curveBuyFeeBps;
    /// @notice Protocol fee on curve sells, taken out of the proceeds.
    uint16 public immutable curveSellFeeBps;

    /// @notice The four-hex mark every launched token carries, matched at
    ///         EITHER end of the address: `0x2add…` or `0x…2add` — "2-add",
    ///         double plus. Both ends are accepted because it halves the
    ///         client's search (two targets, same odds each) and truncated
    ///         displays show both ends anyway, so the mark stays visible
    ///         whichever way it lands.
    ///
    ///         Immutable rather than a compile-time constant so tests can
    ///         deploy a cheap mark and skip the CREATE2 grind entirely; every
    ///         real deployment passes 0x2add. The checksum case (2add vs 2ADD)
    ///         is a display convention derived off-chain from the address hash
    ///         and cannot be enforced here — miners additionally require the
    ///         tail to be all-lower or all-upper so no token reads 0x…2AdD.
    uint16 public immutable vanity;

    bool public launchesPaused;

    /// @notice Flat fee to open a listing. Prices out spam as much as it earns.
    uint256 public creationFeeWei;
    /// @notice Open-mode graduation trigger, in raised wei. Frozen per listing
    ///         at launch so a live curve never has its finish line moved. The
    ///         optimum is empirical, hence settable rather than immutable.
    ///         4 ETH graduates at roughly 13 ETH FDV (3.33x the raise less the
    ///         start FDV), in line with Robinhood Chain's leading launchpad
    ///         (4.2 ETH); 0.5 ETH graduated at ~1.3 ETH FDV, a coin too small
    ///         to register on any tracker. Testnets lower it with setParams.
    uint256 public graduationRaiseWei = 4 ether;
    /// @notice How long an aborted raise's escrow stays claimable.
    uint64 public sweepDelaySecs = 365 days;
    /// @notice Open-mode creator's share of the curve fee, in bps of the fee.
    uint16 public creatorCurveShareBps = 1_000;

    /// @notice Pull-payment ledger. Nothing in the trade path makes an external
    ///         call to a fee recipient: a treasury that cannot receive ETH must
    ///         never be able to brick every buy on the platform.
    mapping(address recipient => uint256 wei_) public feesAccrued;

    /// @notice `Guaranteed` is all-or-nothing with a target, a deadline and a
    ///         full refund on failure; curve sells are capped at the seller's
    ///         cost basis so the refund stays funded. `Open` has no target, no
    ///         deadline and no refund, graduates on `graduationRaiseWei`, and
    ///         lets the curve run uncapped in both directions.
    enum RaiseMode { Guaranteed, Open }

    struct Listing {
        address creator;
        address pair;
        uint16 taxBps;
        uint64 createdAt;
        bytes32 poolId; // 0 until graduated
    }

    struct Curve {
        uint128 basePriceWei; // p0: wei per whole token at zero sold
        uint128 slopeQ;       // k (Q18): wei-per-whole-token increase per whole token sold
        uint64 deadline;
        bool finalized;
        bool aborted;
        uint16 founderRaiseBps;
        uint256 soldWhole;    // whole tokens sold on the curve
        uint256 raisedWei;
        uint256 targetRaiseWei;
        uint256 maxBuyWei;    // per-wallet spend cap
        RaiseMode mode;
        uint64 abortedAt;     // 0 until abort(); starts the sweep clock
        bool swept;
        bytes v3Path;         // WETH -> ... -> pair (empty when pair IS WETH)
    }

    struct Position {
        int24 tickLower;
        int24 tickUpper;
        uint128 liquidity;
    }

    mapping(address token => Listing) public listings;
    mapping(address token => Curve) internal _curves;
    mapping(address token => address) public vestingOf;
    mapping(address token => VentureFeeHook.FeePolicy) public feePolicyOf;
    /// @notice What was locked into the pool at graduation, kept as a public
    ///         record so anyone can read back the exact tick range and
    ///         liquidity. Nothing in this contract can withdraw it.
    mapping(address token => Position) public tokenPositions;
    mapping(address token => Position) public pairPositions;
    mapping(address token => mapping(address buyer => uint256)) public spentWei;
    mapping(address token => mapping(address buyer => uint256)) public boughtTokens; // token-wei
    address[] public allTokens;
    mapping(address creator => address[]) internal _tokensByCreator;

    struct LaunchParams {
        string name;
        string symbol;
        string metadataURI; // JSON: description, logo, links + pitch/sector
        address pair;       // trading quote + dividend currency
        uint16 buyTaxBps;   // 0..400: founder tax on buys after graduation
        uint16 sellTaxBps;  // 0..400: founder tax on sells after graduation
        address devWallet;  // dev-fee recipient; 0 => the founder
        uint16 devBps;      // the four buckets split the founder tax and
        uint16 dividendBps; // must sum to 10_000
        uint16 liquidityBps;
        uint16 mmBps;
        uint256 ethUsdPrice8;    // ETH USD price, 8dp (sizes the curve start)
        uint256 targetRaiseWei;  // graduation trigger
        uint64 raiseDurationSecs;
        uint256 maxBuyWei;       // 0 => targetRaiseWei / 50
        uint16 founderRaiseBps;  // 0..3000: cut of the raise paid at graduation
        uint16 founderSupplyBps; // 0..1500: vested founder allocation
        uint32 vestingSecs;      // required when founderSupplyBps > 0
        RaiseMode mode;          // Guaranteed (AON raise) or Open (free curve)
        uint256 minHoldForDividends; // whole-token floor to earn dividends; 0 = everyone
        uint8 dividendMode;      // 0 linear, 1 tiered (bigger holdings earn more per token)
        bytes v3Path;            // WETH -> ... -> pair route for finalize
    }

    event Launched(
        address indexed token,
        address indexed creator,
        address indexed pair,
        uint16 taxBps,
        uint256 targetRaiseWei,
        uint64 deadline,
        address vesting
    );
    event CurveBuy(address indexed token, address indexed buyer, uint256 ethIn, uint256 tokensOut, uint128 priceWei);
    event Graduated(
        address indexed token, bytes32 poolId, uint256 raisedWei, uint256 founderCutWei, uint256 pairSeeded
    );
    event Aborted(address indexed token, uint256 raisedWei, uint256 burned);
    event Refunded(address indexed token, address indexed buyer, uint256 ethOut, uint256 tokensReturned);
    event CurveSell(
        address indexed token, address indexed seller, uint256 ethOut, uint256 tokensIn, uint128 priceWei, uint256 feeWei
    );
    event FeeAccrued(address indexed token, address indexed recipient, uint256 amount);
    event FeesWithdrawn(address indexed recipient, uint256 amount);
    event Swept(address indexed token, uint256 amount);
    event ParamsSet(uint256 creationFeeWei, uint256 graduationRaiseWei, uint64 sweepDelaySecs, uint16 creatorCurveShareBps);
    event LaunchesPausedSet(bool paused);

    error LaunchesPaused_();
    error InvalidParams();
    error BadVanity();
    error NotProtocolAdmin();
    error CurveClosed();
    error CurveLive();
    error AlreadyFinalized();
    error NotAborted();
    error CapExceeded();
    error NothingToRefund();
    error NothingToSell();
    error SlippageExceeded();
    error SweepTooEarly();
    error AlreadySwept();
    error FeeTooHigh();
    error EthTransferFailed();
    error NotPoolManager();

    modifier onlyProtocolAdmin() {
        if (msg.sender != protocolAdmin) revert NotProtocolAdmin();
        _;
    }

    constructor(
        address owner_,
        address protocolAdmin_,
        IPoolManager poolManager_,
        VentureFeeHook hook_,
        address weth_,
        ISwapRouterV3V v3Router_,
        VestingDeployer vestingDeployer_,
        VentureTokenDeployer tokenDeployer_,
        uint16 curveBuyFeeBps_,
        uint16 curveSellFeeBps_,
        uint256 minTargetWei_,
        uint16 vanity_,
        uint64 minEthUsd8_,
        uint64 maxEthUsd8_
    ) Ownable(owner_) {
        require(protocolAdmin_ != address(0), "admin=0");
        if (minTargetWei_ == 0 || minTargetWei_ > MAX_TARGET_WEI) revert InvalidParams();
        minTargetWei = minTargetWei_;
        // The trigger is never below the floor, whatever the floor is.
        if (graduationRaiseWei < minTargetWei_) graduationRaiseWei = minTargetWei_;
        if (minEthUsd8_ == 0 || minEthUsd8_ > maxEthUsd8_) revert InvalidParams();
        minEthUsd8 = minEthUsd8_;
        maxEthUsd8 = maxEthUsd8_;
        if (curveBuyFeeBps_ > MAX_CURVE_FEE_BPS || curveSellFeeBps_ > MAX_CURVE_FEE_BPS) revert FeeTooHigh();
        curveBuyFeeBps = curveBuyFeeBps_;
        curveSellFeeBps = curveSellFeeBps_;
        vanity = vanity_;
        protocolAdmin = protocolAdmin_;
        poolManager = poolManager_;
        hook = hook_;
        weth = IWETH9V(weth_);
        v3Router = v3Router_;
        vestingDeployer = vestingDeployer_;
        tokenDeployer = tokenDeployer_;
    }

    // ---------------------------------------------------------------------
    // Admin
    // ---------------------------------------------------------------------

    function pause() external onlyProtocolAdmin {
        launchesPaused = true;
        emit LaunchesPausedSet(true);
    }

    function resume() external onlyProtocolAdmin {
        launchesPaused = false;
        emit LaunchesPausedSet(false);
    }

    /// @notice Tune the parameters whose optimum is empirical. The curve fees
    ///         themselves are immutable; these are not, because a graduation
    ///         threshold guessed before launch can never be the right one.
    ///         Applies to new launches only: `graduationRaiseWei` is copied
    ///         into the curve at launch, so no live raise moves.
    function setParams(
        uint256 creationFeeWei_,
        uint256 graduationRaiseWei_,
        uint64 sweepDelaySecs_,
        uint16 creatorCurveShareBps_
    ) external onlyProtocolAdmin {
        if (graduationRaiseWei_ < minTargetWei || graduationRaiseWei_ > MAX_TARGET_WEI) revert InvalidParams();
        if (sweepDelaySecs_ < MIN_SWEEP_DELAY) revert InvalidParams();
        if (creatorCurveShareBps_ > 5_000) revert InvalidParams();
        creationFeeWei = creationFeeWei_;
        graduationRaiseWei = graduationRaiseWei_;
        sweepDelaySecs = sweepDelaySecs_;
        creatorCurveShareBps = creatorCurveShareBps_;
        emit ParamsSet(creationFeeWei_, graduationRaiseWei_, sweepDelaySecs_, creatorCurveShareBps_);
    }

    // ---------------------------------------------------------------------
    // Fee ledger: accrue in the trade path, withdraw out of band
    // ---------------------------------------------------------------------

    function _accrue(address token, address to, uint256 amount) internal {
        if (amount == 0 || to == address(0)) return;
        feesAccrued[to] += amount;
        emit FeeAccrued(token, to, amount);
    }

    /// @dev A curve fee, less the hook's referee discount when the caller has a
    ///      bound referrer: the same rule the hook applies after graduation.
    function _feeFor(uint256 amount, uint16 bps) internal view returns (uint256 fee) {
        fee = (amount * bps) / BPS;
        if (hook.referrerOf(msg.sender) != address(0)) fee -= (fee * hook.REFEREE_DISCOUNT_BPS()) / BPS;
    }

    /// @dev Referrer first, then the Open-mode creator's share, remainder to
    ///      the protocol treasury. Nothing here calls out to the recipients.
    function _splitCurveFee(address token, uint256 fee) internal {
        if (fee == 0) return;
        uint256 remaining = fee;
        address ref = hook.referrerOf(msg.sender);
        if (ref != address(0) && ref != msg.sender) {
            uint256 toRef = (fee * hook.refShareBps()) / BPS;
            _accrue(token, ref, toRef);
            remaining -= toRef;
        }
        if (_curves[token].mode == RaiseMode.Open && creatorCurveShareBps > 0) {
            uint256 toCreator = (fee * creatorCurveShareBps) / BPS;
            _accrue(token, listings[token].creator, toCreator);
            remaining -= toCreator;
        }
        _accrue(token, hook.platformTreasury(), remaining);
    }

    /// @notice Withdraw everything accrued to the caller.
    function withdrawFees() external nonReentrant returns (uint256 amount) {
        amount = feesAccrued[msg.sender];
        if (amount == 0) revert NothingToRefund();
        feesAccrued[msg.sender] = 0;
        (bool ok,) = payable(msg.sender).call{value: amount}("");
        if (!ok) revert EthTransferFailed();
        emit FeesWithdrawn(msg.sender, amount);
    }

    // ---------------------------------------------------------------------
    // Launch: deploy the token, escrow the founder allocation, open the curve
    // ---------------------------------------------------------------------

    function launch(LaunchParams calldata p, bytes32 salt) external payable nonReentrant returns (address token) {
        if (launchesPaused) revert LaunchesPaused_();
        if (msg.value < creationFeeWei) revert InvalidParams();
        if (bytes(p.name).length == 0 || bytes(p.symbol).length == 0) revert InvalidParams();
        if (p.buyTaxBps > hook.MAX_SIDE_TAX_BPS() || p.sellTaxBps > hook.MAX_SIDE_TAX_BPS()) revert InvalidParams();
        if (uint256(p.devBps) + p.dividendBps + p.liquidityBps + p.mmBps != BPS) revert InvalidParams();
        if (p.ethUsdPrice8 < minEthUsd8 || p.ethUsdPrice8 > maxEthUsd8) revert InvalidParams();
        // WETH pairs only, for now. A stock-paired coin graduates, but trading
        // it through the router fails on mainnet with CurrencyNotSettled from
        // the hook's fee plumbing in the stock currency (venture.fork.test.ts).
        // The interface already hid stock pairs; this stops a direct launch
        // creating a coin nobody can buy. Remove once the hook path is fixed.
        if (p.pair != address(weth)) revert InvalidParams();
        bool open = p.mode == RaiseMode.Open;
        // Open mode has no deadline and no cut of a raise: the creator is paid
        // out of curve fees instead, so there is nothing to hold to a target.
        if (!open && (p.raiseDurationSecs < MIN_RAISE_SECS || p.raiseDurationSecs > MAX_RAISE_SECS)) {
            revert InvalidParams();
        }
        if (open && p.founderRaiseBps != 0) revert InvalidParams();
        if (p.founderRaiseBps > MAX_FOUNDER_RAISE_BPS) revert InvalidParams();
        if (p.founderSupplyBps > MAX_FOUNDER_SUPPLY_BPS) revert InvalidParams();
        if (p.founderSupplyBps > 0 && (p.vestingSecs < MIN_VESTING_SECS || p.vestingSecs > MAX_VESTING_SECS)) {
            revert InvalidParams();
        }
        // One trigger serves both modes: Guaranteed uses the founder's target,
        // Open takes the protocol's graduation threshold, frozen here.
        uint256 target = open ? graduationRaiseWei : p.targetRaiseWei;
        if (target > MAX_TARGET_WEI) revert InvalidParams();
        // The floor is a promise about pool depth, so it binds the ETH that
        // actually becomes liquidity, not the headline raise. finalize() pays
        // founderRaiseBps out first and seeds the pool with the remainder, so a
        // floor checked against the gross let a floor-sized raise with the
        // maximum cut graduate into 70% of the floor — a pool too thin to
        // absorb the quick-buy amounts the interface offers. Open mode forces
        // founderRaiseBps to 0 above, so there the two are the same number.
        if (target - (target * p.founderRaiseBps) / BPS < minTargetWei) revert InvalidParams();

        // p0: whole-supply FDV of START_MCAP_USD at the first curve buy.
        uint256 p0 = Math.mulDiv(START_MCAP_USD_8, 1e18, TOTAL_SUPPLY_WHOLE * p.ethUsdPrice8);
        // The 1e15 ceiling (start FDV <= 1M ETH) keeps every curve-math
        // intermediate comfortably inside uint256 for the life of the raise.
        if (p0 == 0 || p0 > 1e15) revert InvalidParams();

        // k (Q18) such that selling the whole curve raises exactly the target:
        // target = p0*C + k*C^2/2  =>  k = 2*(target - p0*C)/C^2.
        uint256 baseCost = p0 * CURVE_SUPPLY_WHOLE;
        if (target < baseCost) revert InvalidParams(); // curve slope must be >= 0
        uint256 slopeQ =
            Math.mulDiv(2 * (target - baseCost), 1e18, CURVE_SUPPLY_WHOLE * CURVE_SUPPLY_WHOLE);
        if (slopeQ > type(uint128).max) revert InvalidParams();

        // A per-wallet cap is a fairness device for an all-or-nothing raise.
        // Open mode is a market; it does not have one.
        uint256 maxBuy = open ? type(uint256).max : (p.maxBuyWei == 0 ? target / 50 : p.maxBuyWei);
        if (maxBuy < target / 200) revert InvalidParams(); // cap can't make the raise impossible

        // Dividend-policy coherence is checked by the deployer: the factory is
        // at its bytecode ceiling and the deployer has room to spare.
        token = tokenDeployer.deployToken(
            salt, p.name, p.symbol, p.metadataURI, TOTAL_SUPPLY, msg.sender, p.buyTaxBps, p.pair,
            p.minHoldForDividends, p.dividendMode, p.dividendBps
        );
        // doubleplus tokens end 0x…2add — "2-add", double plus. The checksum
        // case (2add vs 2ADD) is a display convention computed off-chain from
        // the address hash, so it cannot be enforced here; the miners additionally
        // require the tail to be all-lower or all-upper, never mixed. A salt
        // mined without that extra constraint still passes this check, which is
        // fine: the casing is branding, not a security property.
        uint160 a = uint160(token);
        // vanity == 0 disables the mark entirely, which is how the test suite
        // skips the CREATE2 grind. A smaller mark would not help: any non-zero
        // value is still a 16-bit compare, so 0x000f costs exactly what 0x2add
        // does. Every real deployment passes 0x2add; the value is public.
        if (vanity != 0 && a & 0xffff != vanity && a >> 144 != vanity) revert BadVanity();
        if (token == p.pair) revert InvalidParams();

        // Vesting escrow first, so it can go on the dividend exclusion list.
        address vesting;
        if (p.founderSupplyBps > 0) {
            vesting = address(vestingDeployer.deploy(token, msg.sender, uint64(p.vestingSecs)));
            vestingOf[token] = vesting;
        }

        address[] memory ex = new address[](vesting == address(0) ? 2 : 3);
        ex[0] = address(poolManager);
        ex[1] = address(this);
        if (vesting != address(0)) ex[2] = vesting;
        tokenDeployer.initHook(token, address(hook), ex);

        if (vesting != address(0)) {
            IERC20(token).safeTransfer(vesting, (TOTAL_SUPPLY * p.founderSupplyBps) / BPS);
        }

        _curves[token] = Curve({
            basePriceWei: uint128(p0),
            slopeQ: uint128(slopeQ),
            deadline: open ? type(uint64).max : uint64(block.timestamp) + p.raiseDurationSecs,
            finalized: false,
            aborted: false,
            founderRaiseBps: p.founderRaiseBps,
            soldWhole: 0,
            raisedWei: 0,
            targetRaiseWei: target,
            maxBuyWei: maxBuy,
            mode: p.mode,
            abortedAt: 0,
            swept: false,
            v3Path: p.v3Path
        });
        feePolicyOf[token] = VentureFeeHook.FeePolicy({
            devWallet: p.devWallet == address(0) ? msg.sender : p.devWallet,
            buyTaxBps: p.buyTaxBps,
            sellTaxBps: p.sellTaxBps,
            devBps: p.devBps,
            dividendBps: p.dividendBps,
            liquidityBps: p.liquidityBps,
            mmBps: p.mmBps
        });
        listings[token] =
            Listing({creator: msg.sender, pair: p.pair, taxBps: p.buyTaxBps, createdAt: uint64(block.timestamp), poolId: 0});
        allTokens.push(token);
        _tokensByCreator[msg.sender].push(token);

        if (creationFeeWei > 0) _accrue(token, hook.platformTreasury(), creationFeeWei);
        if (msg.value > creationFeeWei) {
            (bool back,) = payable(msg.sender).call{value: msg.value - creationFeeWei}("");
            if (!back) revert EthTransferFailed();
        }

        emit Launched(token, msg.sender, p.pair, p.buyTaxBps, target, _curves[token].deadline, vesting);
    }

    // ---------------------------------------------------------------------
    // Curve math
    // ---------------------------------------------------------------------

    /// @notice Current curve price, wei per whole token.
    function priceNow(address token) public view returns (uint128) {
        Curve storage c = _curves[token];
        if (c.basePriceWei == 0) revert InvalidParams();
        return uint128(uint256(c.basePriceWei) + Math.mulDiv(c.slopeQ, c.soldWhole, 1e18));
    }

    /// @notice Exact integral cost, in wei, of buying `qWhole` whole tokens
    ///         starting from `fromSoldWhole` sold.
    function curveCost(address token, uint256 qWhole, uint256 fromSoldWhole) public view returns (uint256) {
        Curve storage c = _curves[token];
        // q*p0 + k*(S*q + q^2/2), computed as k*(2Sq + q^2)/2 in Q18.
        return qWhole * c.basePriceWei
            + Math.mulDiv(c.slopeQ, 2 * fromSoldWhole * qWhole + qWhole * qWhole, 2e18);
    }

    /// @notice Whole tokens bought by spending `valueWei` from the current
    ///         point on the curve — the closed-form inverse of curveCost, so
    ///         pricing is path-independent (order splitting changes nothing).
    function tokensForValue(address token, uint256 valueWei) public view returns (uint256 qWhole) {
        Curve storage c = _curves[token];
        uint256 k = c.slopeQ;
        if (k == 0) return valueWei / c.basePriceWei;
        // Solve (k/2)q^2 + b*q - 1e18*v = 0 with b = 1e18*p0 + k*S.
        uint256 b = 1e18 * uint256(c.basePriceWei) + k * c.soldWhole;
        uint256 disc = b * b + 2 * k * (1e18 * valueWei);
        qWhole = (Math.sqrt(disc) - b) / k;
    }

    // ---------------------------------------------------------------------
    // Buy on the curve
    // ---------------------------------------------------------------------

    /// @param minTokensOut Least the buyer accepts, in token-wei. Another buy
    ///        landing first moves the price up the curve, so without a floor a
    ///        buy can fill at a price the buyer never saw quoted.
    function buy(address token, uint256 minTokensOut) external payable nonReentrant returns (uint256 tokensOut) {
        Curve storage c = _curves[token];
        if (c.basePriceWei == 0) revert InvalidParams();
        if (c.finalized || c.aborted) revert CurveClosed();
        if (block.timestamp >= c.deadline && c.raisedWei < c.targetRaiseWei) revert CurveClosed();
        if (c.raisedWei >= c.targetRaiseWei || c.soldWhole >= CURVE_SUPPLY_WHOLE) revert CurveClosed();
        if (msg.value == 0) revert InvalidParams();

        // The entry fee comes off the incoming value before the curve is
        // quoted, so spentWei records what actually reaches escrow. Booking it
        // gross would leave refund liability above escrow by exactly the take.
        uint256 feeIn = _feeFor(msg.value, curveBuyFeeBps);
        uint256 netValue = msg.value - feeIn;
        if (netValue == 0) revert InvalidParams();

        uint256 q = tokensForValue(token, netValue);
        uint256 remaining = CURVE_SUPPLY_WHOLE - c.soldWhole;
        if (q > remaining) q = remaining;
        if (q == 0) revert InvalidParams();

        uint256 spend = curveCost(token, q, c.soldWhole) + 1; // round the cost up
        if (spend > netValue) spend = netValue;
        uint256 cap = block.timestamp < listings[token].createdAt + LAUNCH_WINDOW_SECS
            ? Math.min(c.maxBuyWei, c.targetRaiseWei / LAUNCH_CAP_DIV)
            : c.maxBuyWei;
        if (spentWei[token][msg.sender] + spend > cap) revert CapExceeded();

        uint128 price = priceNow(token);
        c.soldWhole += q;
        c.raisedWei += spend;
        spentWei[token][msg.sender] += spend;
        tokensOut = q * 1e18;
        if (tokensOut < minTokensOut) revert SlippageExceeded();
        boughtTokens[token][msg.sender] += tokensOut;

        IERC20(token).safeTransfer(msg.sender, tokensOut);
        _splitCurveFee(token, feeIn);
        emit CurveBuy(token, msg.sender, spend, tokensOut, price);
        // The buy that fills the raise graduates it in the same transaction.
        // A separate finalize() left a funded curve frozen — no buys, no
        // sells, no pool — until someone paid to push it over, which is the
        // moment a launch has the most attention. The filling buyer pays the
        // graduation gas; finalize() stays as the fallback for any curve that
        // reached its target without one (e.g. within rounding tolerance).
        if (_reached(token)) _graduate(token);
        if (netValue > spend) {
            (bool ok,) = payable(msg.sender).call{value: netValue - spend}("");
            if (!ok) revert EthTransferFailed();
        }
    }

    // ---------------------------------------------------------------------
    // Sell back to the curve
    // ---------------------------------------------------------------------

    /// @notice Return `qWhole` tokens to the curve for ETH. The claim belongs
    ///         to the address that bought on the curve: tokens acquired by
    ///         plain transfer carry no curve position, exactly as with refund().
    /// @dev    In Guaranteed mode the payout is capped at the seller's pro-rata
    ///         cost basis. That cap is a solvency requirement, not a policy:
    ///         escrow is `B - S` and refund liability is `B - C`, so refunds
    ///         stay funded iff `C >= S`. Uncapped, an early buyer could sell
    ///         into later buyers' ETH and leave the rest short.
    function sell(address token, uint256 qWhole, uint256 minEthOut)
        external
        nonReentrant
        returns (uint256 ethOut)
    {
        Curve storage c = _curves[token];
        if (c.basePriceWei == 0) revert InvalidParams();
        if (c.finalized || c.aborted) revert CurveClosed();
        // High-water lock: once the graduation trigger is crossed the curve is
        // frozen both ways, so a sell cannot drag a funded raise back under it.
        if (c.raisedWei >= c.targetRaiseWei || c.soldWhole >= CURVE_SUPPLY_WHOLE) revert CurveClosed();

        uint256 owned = boughtTokens[token][msg.sender];
        if (owned == 0 || qWhole == 0) revert NothingToSell();
        uint256 qMax = owned / 1e18;
        uint256 q = qWhole > qMax ? qMax : qWhole;
        if (q == 0 || q > c.soldWhole) revert NothingToSell();

        uint256 tokenWei = q * 1e18;
        uint256 costBasis = Math.mulDiv(spentWei[token][msg.sender], tokenWei, owned);
        uint256 gross = curveCost(token, q, c.soldWhole - q);
        if (c.mode == RaiseMode.Guaranteed && gross > costBasis) gross = costBasis;
        if (gross > c.raisedWei) gross = c.raisedWei;

        uint256 fee = _feeFor(gross, curveSellFeeBps);
        ethOut = gross - fee;
        if (ethOut < minEthOut) revert SlippageExceeded();

        spentWei[token][msg.sender] -= costBasis;
        boughtTokens[token][msg.sender] = owned - tokenWei;
        c.soldWhole -= q;
        c.raisedWei -= gross;

        // Tokens go back to factory inventory so the curve can resell them.
        IERC20(token).safeTransferFrom(msg.sender, address(this), tokenWei);
        _splitCurveFee(token, fee);
        (bool ok,) = payable(msg.sender).call{value: ethOut}("");
        if (!ok) revert EthTransferFailed();
        emit CurveSell(token, msg.sender, ethOut, tokenWei, priceNow(token), fee);
    }

    // ---------------------------------------------------------------------
    // Graduation: target hit -> pay the founder cut, seed the locked pool
    // ---------------------------------------------------------------------

    function finalize(address token) external nonReentrant returns (bytes32 poolId) {
        Curve storage c = _curves[token];
        if (c.basePriceWei == 0) revert InvalidParams();
        if (c.finalized) revert AlreadyFinalized();
        if (c.aborted) revert CurveClosed();
        if (!_reached(token)) revert CurveLive();
        return _graduate(token);
    }

    /// @dev Whether the raise counts as filled: target raised, curve sold out,
    ///      or within one whole token of the target.
    ///
    ///      One whole token of tolerance on the target, because the curve only
    ///      moves in whole tokens. A buy books curveCost(q)+1 for the largest
    ///      whole q its value covers and refunds the rest, so every buy can leave
    ///      up to one token's price unbooked; filling a raise in slices lands
    ///      short by that much. Observed live: a 2 ETH raise filled in three
    ///      slices stopped 6.26 gwei short, and the wallet that filled it could
    ///      not close the gap either, because its per-wallet cap had exactly that
    ///      much headroom while the buy needed one wei more.
    ///
    ///      A different wallet could still close it, so this is a tolerance, not
    ///      an impossibility proof: a raise within one token of target counts as
    ///      reached rather than waiting on a stranger to spend a few gwei, or
    ///      running to its deadline and refunding everyone over rounding dust.
    ///      The shortfall is bounded by the price of one token.
    function _reached(address token) internal view returns (bool) {
        Curve storage c = _curves[token];
        if (c.raisedWei >= c.targetRaiseWei || c.soldWhole >= CURVE_SUPPLY_WHOLE) return true;
        return c.targetRaiseWei - c.raisedWei <= curveCost(token, 1, c.soldWhole);
    }

    /// @dev Pay the founder cut, seed the locked pool, start vesting. Callers
    ///      have checked the curve is live and `_reached`.
    function _graduate(address token) internal returns (bytes32 poolId) {
        Curve storage c = _curves[token];
        Listing storage l = listings[token];
        c.finalized = true;

        // 1) Founder's declared cut of the raise, credited to the founder's
        //    fee balance and pulled with withdrawFees(). It used to be pushed
        //    here, so a creator contract that rejects ETH made finalize()
        //    revert forever, and abort() is closed once the target is
        //    crossed: every backer's escrow stuck with no exit. Crediting it
        //    means no recipient can block graduation.
        uint256 founderCut = (c.raisedWei * c.founderRaiseBps) / BPS;
        if (founderCut > 0) _accrue(token, l.creator, founderCut);

        // 2) Remaining ETH -> pair token (liquidity side).
        uint256 poolEth = c.raisedWei - founderCut;
        address pair = l.pair;
        uint256 pairAmount;
        if (poolEth > 0) {
            weth.deposit{value: poolEth}();
            if (pair == address(weth)) {
                pairAmount = poolEth;
            } else {
                IERC20(address(weth)).forceApprove(address(v3Router), poolEth);
                pairAmount = v3Router.exactInput(
                    ISwapRouterV3V.ExactInputParams({
                        path: c.v3Path,
                        recipient: address(this),
                        amountIn: poolEth,
                        amountOutMinimum: 0
                    })
                );
            }
        }

        // 3) Closing curve price, converted to pair units per whole token.
        uint256 priceEnd = uint256(c.basePriceWei) + Math.mulDiv(c.slopeQ, c.soldWhole, 1e18);
        uint256 priceQ = (pair == address(weth) || pairAmount == 0 || poolEth == 0)
            ? priceEnd
            : Math.mulDiv(priceEnd, pairAmount, poolEth);
        if (priceQ == 0) revert InvalidParams();

        bool tokenIsCurrency0 = token < pair;
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(tokenIsCurrency0 ? token : pair),
            currency1: Currency.wrap(tokenIsCurrency0 ? pair : token),
            fee: LP_FEE,
            tickSpacing: TICK_SPACING,
            hooks: IHooks(address(hook))
        });

        (uint160 sqrtPriceX96, int24 alignedEdge) = _poolStart(tokenIsCurrency0, priceQ);
        poolManager.initialize(key, sqrtPriceX96);

        int24 minTick = (TickMath.MIN_TICK / TICK_SPACING) * TICK_SPACING;
        int24 maxTick = (TickMath.MAX_TICK / TICK_SPACING) * TICK_SPACING;

        // 4a) Everything the factory still holds: single-sided token liquidity.
        uint256 unsold = IERC20(token).balanceOf(address(this));
        if (unsold > 0) {
            (int24 tl, int24 tu) = tokenIsCurrency0 ? (alignedEdge + TICK_SPACING, maxTick) : (minTick, alignedEdge);
            uint128 liq = tokenIsCurrency0
                ? LiquidityAmounts.getLiquidityForAmount0(
                    TickMath.getSqrtPriceAtTick(tl), TickMath.getSqrtPriceAtTick(tu), unsold
                )
                : LiquidityAmounts.getLiquidityForAmount1(
                    TickMath.getSqrtPriceAtTick(tl), TickMath.getSqrtPriceAtTick(tu), unsold
                );
            if (liq > 0) {
                tokenPositions[token] = Position({tickLower: tl, tickUpper: tu, liquidity: liq});
                poolManager.unlock(abi.encode(key, tl, tu, liq));
            }
        }

        // 4b) Raised pair tokens: single-sided buy support under the price.
        if (pairAmount > 0) {
            (int24 tl, int24 tu) = tokenIsCurrency0 ? (minTick, alignedEdge) : (alignedEdge + TICK_SPACING, maxTick);
            uint128 liq = tokenIsCurrency0
                ? LiquidityAmounts.getLiquidityForAmount1(
                    TickMath.getSqrtPriceAtTick(tl), TickMath.getSqrtPriceAtTick(tu), pairAmount
                )
                : LiquidityAmounts.getLiquidityForAmount0(
                    TickMath.getSqrtPriceAtTick(tl), TickMath.getSqrtPriceAtTick(tu), pairAmount
                );
            if (liq > 0) {
                pairPositions[token] = Position({tickLower: tl, tickUpper: tu, liquidity: liq});
                poolManager.unlock(abi.encode(key, tl, tu, liq));
            }
        }

        poolId = PoolId.unwrap(key.toId());
        l.poolId = poolId;
        hook.registerPool(key, token, pair, feePolicyOf[token], tokenIsCurrency0);

        // 5) Start the founder's vesting clock.
        address vesting = vestingOf[token];
        if (vesting != address(0)) FounderVesting(vesting).start();

        emit Graduated(token, poolId, c.raisedWei, founderCut, pairAmount);
    }

    // ---------------------------------------------------------------------
    // Failure path: deadline passed below target -> abort and refund
    // ---------------------------------------------------------------------

    /// @notice Anyone can flip a dead raise into the refundable state. Burns
    ///         the founder's escrowed allocation and the factory's remaining
    ///         inventory; buyers then pull refunds individually.
    function abort(address token) external nonReentrant {
        Curve storage c = _curves[token];
        if (c.basePriceWei == 0) revert InvalidParams();
        if (c.finalized) revert AlreadyFinalized();
        if (c.aborted) revert CurveClosed();
        if (block.timestamp < c.deadline) revert CurveLive();
        if (c.raisedWei >= c.targetRaiseWei || c.soldWhole >= CURVE_SUPPLY_WHOLE) revert CurveLive();
        c.aborted = true;
        c.abortedAt = uint64(block.timestamp);

        address vesting = vestingOf[token];
        if (vesting != address(0)) FounderVesting(vesting).reclaim();

        QuiverToken qt = QuiverToken(payable(token));
        uint256 held = qt.balanceOf(address(this));
        if (held > 0) qt.burn(held);
        emit Aborted(token, c.raisedWei, held);
    }

    /// @notice Return the full bought amount, get the full spend back. Tokens
    ///         must be approved to the factory first; they are burned.
    function refund(address token) external nonReentrant returns (uint256 ethOut) {
        Curve storage c = _curves[token];
        if (!c.aborted) revert NotAborted();
        if (c.swept) revert AlreadySwept();
        uint256 tokensBack = boughtTokens[token][msg.sender];
        ethOut = spentWei[token][msg.sender];
        if (tokensBack == 0 || ethOut == 0) revert NothingToRefund();
        boughtTokens[token][msg.sender] = 0;
        spentWei[token][msg.sender] = 0;
        // Keep raisedWei equal to the ETH this raise still holds, so the two
        // ledgers stay in step and sweepUnclaimed knows what is genuinely left.
        c.raisedWei -= ethOut;

        IERC20(token).safeTransferFrom(msg.sender, address(this), tokensBack);
        QuiverToken(payable(token)).burn(tokensBack);

        (bool ok,) = payable(msg.sender).call{value: ethOut}("");
        if (!ok) revert EthTransferFailed();
        emit Refunded(token, msg.sender, ethOut, tokensBack);
    }

    /// @notice Sweep escrow left behind long after an aborted raise. A curve
    ///         claim is keyed to the buying address, so a backer who moves
    ///         their tokens away can no longer refund and their ETH would
    ///         otherwise sit here forever. The window is deliberately long and
    ///         the claim page stays open for all of it.
    function sweepUnclaimed(address token) external nonReentrant returns (uint256 amount) {
        Curve storage c = _curves[token];
        if (!c.aborted) revert NotAborted();
        if (c.swept) revert AlreadySwept();
        if (block.timestamp < uint256(c.abortedAt) + sweepDelaySecs) revert SweepTooEarly();
        amount = c.raisedWei;
        c.swept = true;
        c.raisedWei = 0;
        _accrue(token, hook.platformTreasury(), amount);
        emit Swept(token, amount);
    }

    // ---------------------------------------------------------------------
    // PoolManager callback: settle whatever the add owes
    // ---------------------------------------------------------------------

    /// @notice Add-only by construction. `liquidityDelta` is built from a
    ///         uint128 and cast positive, so there is no encoding of this
    ///         payload that removes liquidity — not merely no caller that
    ///         does. Graduated liquidity has no path out of the pool.
    function unlockCallback(bytes calldata data) external override returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        (PoolKey memory key, int24 tl, int24 tu, uint128 liq) = abi.decode(data, (PoolKey, int24, int24, uint128));
        (BalanceDelta delta,) = poolManager.modifyLiquidity(
            key,
            ModifyLiquidityParams({tickLower: tl, tickUpper: tu, liquidityDelta: int256(uint256(liq)), salt: bytes32(0)}),
            ""
        );
        _settleNegative(key.currency0, delta.amount0());
        _settleNegative(key.currency1, delta.amount1());
        return "";
    }

    function _settleNegative(Currency currency, int128 amount) internal {
        if (amount >= 0) return;
        poolManager.sync(currency);
        IERC20(Currency.unwrap(currency)).safeTransfer(address(poolManager), uint256(uint128(-amount)));
        poolManager.settle();
    }


    // ---------------------------------------------------------------------
    // Pool start pricing (identical to the proven Hammr placement)
    // ---------------------------------------------------------------------

    function _poolStart(bool tokenIsCurrency0, uint256 priceQ)
        internal
        pure
        returns (uint160 sqrtPriceX96, int24 alignedEdge)
    {
        uint160 target = tokenIsCurrency0
            ? uint160(Math.sqrt(Math.mulDiv(priceQ, 1 << 192, 1e18)))
            : uint160(Math.sqrt(Math.mulDiv(1e18, 1 << 192, priceQ)));
        int24 tick = TickMath.getTickAtSqrtPrice(target);
        int24 aligned = (tick / TICK_SPACING) * TICK_SPACING;
        if (tick < 0 && tick % TICK_SPACING != 0) aligned -= TICK_SPACING;
        alignedEdge = aligned;
        sqrtPriceX96 = TickMath.getSqrtPriceAtTick(tokenIsCurrency0 ? aligned : aligned + TICK_SPACING);
        if (!tokenIsCurrency0) alignedEdge = aligned + TICK_SPACING;
    }

    // ---------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------

    function totalTokens() external view returns (uint256) {
        return allTokens.length;
    }

    function tokensByCreator(address creator) external view returns (address[] memory) {
        return _tokensByCreator[creator];
    }

    function curveState(address token)
        external
        view
        returns (
            uint64 deadline,
            uint128 price,
            uint256 soldWhole,
            uint256 remainingWhole,
            uint256 raisedWei,
            uint256 targetRaiseWei,
            bool finalized,
            bool aborted
        )
    {
        Curve storage c = _curves[token];
        deadline = c.deadline;
        price = c.basePriceWei == 0 ? 0 : priceNow(token);
        soldWhole = c.soldWhole;
        remainingWhole = CURVE_SUPPLY_WHOLE - c.soldWhole;
        raisedWei = c.raisedWei;
        targetRaiseWei = c.targetRaiseWei;
        finalized = c.finalized;
        aborted = c.aborted;
    }

    function terms(address token)
        external
        view
        returns (
            uint16 founderRaiseBps,
            uint256 maxBuyWei,
            address vesting,
            uint128 basePriceWei,
            uint128 slopeQ,
            RaiseMode mode,
            bool swept
        )
    {
        Curve storage c = _curves[token];
        founderRaiseBps = c.founderRaiseBps;
        maxBuyWei = c.maxBuyWei;
        vesting = vestingOf[token];
        basePriceWei = c.basePriceWei;
        slopeQ = c.slopeQ;
        mode = c.mode;
        swept = c.swept;
    }

    receive() external payable {}
}
