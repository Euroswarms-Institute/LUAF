// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {BeforeSwapDelta} from "@uniswap/v4-core/src/types/BeforeSwapDelta.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {PoolId, PoolIdLibrary} from "@uniswap/v4-core/src/types/PoolId.sol";
import {SwapParams, ModifyLiquidityParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {SafeCast} from "@uniswap/v4-core/src/libraries/SafeCast.sol";
import {StateLibrary} from "@uniswap/v4-core/src/libraries/StateLibrary.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";
import {LiquidityAmounts} from "@uniswap/v4-periphery/src/libraries/LiquidityAmounts.sol";

import {IQuiverToken} from "../interfaces/IQuiverToken.sol";

/// @title VentureFeeHook
/// @notice Fee engine for the doubleplus startup launchpad. Every graduated
///         venture carries a founder-authored fee policy, written on-chain at
///         launch and immutable after:
///
///           - Separate BUY and SELL taxes, 0–4% each.
///           - A protocol fee (0.5–1%, fixed at hook deploy) charged on every
///             trade ON TOP of the founder taxes, paid straight to the
///             immutable protocol treasury.
///           - The founder tax splits across four buckets that must sum to
///             100%: dev wallet, holder dividends, auto-liquidity, and a
///             market-making bid wall.
///
///         Everything settles inline in `afterSwap` (the StockFeeHookV3
///         pattern, live on Base): no harvest keeper needed.
///
///           - dev        : paid to the founder's devWallet in the fee currency.
///           - dividends  : normalised to the pair token and credited to all
///             holders via QuiverToken.distributeRewards (pull-claimable).
///           - liquidity  : re-added as single-sided liquidity in a band beside
///             the price, permanently hook-owned (a one-way floor deepener).
///           - market-making : split into TWO tight hook-owned bands — a bid
///             wall of the pair token under the price and an ask band of the
///             coin above it. Every trade refills both sides, so the fee
///             stream is a standing spread-tightener; permissionless
///             recenter() migrates stale walls back beside the price.
///
///         Anti-snipe: for the first seconds after graduation trades pay a
///         decaying premium (15% under 5s, 5% under 15s) and the entire
///         premium is routed into the bid wall — snipers fund the floor.
contract VentureFeeHook is IHooks, ReentrancyGuard, IUnlockCallback {
    using PoolIdLibrary for PoolKey;
    using StateLibrary for IPoolManager;
    using SafeCast for uint256;

    uint256 private constant BPS = 10_000;
    uint16 public constant MAX_SIDE_TAX_BPS = 400; // 4% per side, founder-set
    uint16 public constant MIN_PLATFORM_BPS = 50; // 0.5%
    uint16 public constant MAX_PLATFORM_BPS = 100; // 1%
    /// @notice A trader with a bound referrer pays this much less of the
    ///         platform fee (and of the factory's curve fees). Two-sided: the
    ///         referrer earns, and the person they brought pays less.
    uint16 public constant REFEREE_DISCOUNT_BPS = 1_000; // 10% of the fee
    uint256 public constant PAYOUT_GAS = 100_000;
    int24 internal constant LP_WIDTH = 10; // auto-liquidity band, in spacings
    int24 internal constant WALL_WIDTH = 2; // quote walls: tight bands beside price
    int24 internal constant TICK_SPACING = 60; // mirrors the factory's pools
    uint24 internal constant LP_FEE = 0;
    uint64 internal constant SNIPE_T1 = 5; // seconds
    uint64 internal constant SNIPE_T2 = 15;
    /// @notice recenter() only runs once a pool has traded nothing for this
    ///         long. Without it one transaction could push the price, recenter
    ///         the walls beside the pushed price, and trade back into them.
    uint64 public constant RECENTER_QUIET_SECS = 60;
    uint16 internal constant SNIPE_BPS_1 = 1_500; // 15%
    uint16 internal constant SNIPE_BPS_2 = 500; // 5%

    struct FeePolicy {
        address devWallet;
        uint16 buyTaxBps; // 0..400
        uint16 sellTaxBps; // 0..400
        uint16 devBps; // the four buckets sum to 10_000
        uint16 dividendBps;
        uint16 liquidityBps;
        uint16 mmBps;
    }

    struct Band {
        int24 lower;
        int24 upper;
        uint128 liquidity;
    }

    struct Config {
        address coin;
        address pair;
        FeePolicy policy;
        uint64 launchTime;
        bool coinIsCurrency0;
        bool set;
    }

    IPoolManager public immutable poolManager;
    address public immutable platformTreasury;
    address public immutable launcher;
    /// @notice Protocol fee in bps, charged on every trade on top of the
    ///         founder taxes. Fixed for the life of the hook.
    uint16 public immutable platformFeeBps;
    /// @notice Share of the protocol fee paid to a bound referrer, in bps of
    ///         the protocol fee (not of volume). Fixed for the life of the hook.
    uint16 public immutable refShareBps;

    /// @notice One-time, self-bound referral registry: who recruited whom.
    mapping(address user => address referrer) public referrerOf;

    mapping(PoolId => Config) public configOf;
    mapping(address coin => PoolId) internal _poolOf;
    /// @notice When each pool last saw an outside swap (the hook's own
    ///         plumbing swaps don't count).
    mapping(PoolId => uint64) public lastSwapAt;
    error PriceMoving();

    error NotPoolManager();
    error NotLauncher();
    error NotDevWallet();
    error AlreadyConfigured();
    error BadPolicy();
    error HookNotImplemented();

    event PoolConfigured(PoolId indexed id, address indexed coin, FeePolicy policy);
    event FeeTaken(PoolId indexed id, Currency currency, bool isBuy, uint256 platform, uint256 founderTax, uint256 sniper);
    event BucketsSettled(PoolId indexed id, uint256 dev, uint256 dividends, uint256 liquidity, uint256 wall);
    event LiquidityAdded(
        PoolId indexed id, Currency currency, uint256 amount, uint128 liquidity, bool wall, int24 tickLower, int24 tickUpper
    );
    event WallRemoved(PoolId indexed id, int24 tickLower, int24 tickUpper, uint128 liquidity);
    event WallRecentered(PoolId indexed id, uint256 bandsRemoved, uint256 bidPlaced, uint256 askPlaced);
    event DevWalletChanged(address indexed coin, address indexed from, address indexed to);
    event ReferrerBound(address indexed user, address indexed referrer);
    event ReferralPaid(address indexed trader, address indexed referrer, Currency currency, uint256 amount);
    event PayoutDeferred(address indexed to, Currency indexed currency, uint256 amount);

    modifier onlyPoolManager() {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        _;
    }

    constructor(
        IPoolManager _poolManager,
        address _platformTreasury,
        address _launcher,
        uint16 _platformFeeBps,
        uint16 _refShareBps
    ) {
        require(_platformTreasury != address(0) && _launcher != address(0), "zero");
        require(_platformFeeBps >= MIN_PLATFORM_BPS && _platformFeeBps <= MAX_PLATFORM_BPS, "platform bps");
        require(_refShareBps <= 5_000, "ref bps"); // at most half the protocol fee
        poolManager = _poolManager;
        platformTreasury = _platformTreasury;
        launcher = _launcher;
        platformFeeBps = _platformFeeBps;
        refShareBps = _refShareBps;
    }

    // ---------------------------------------------------------------------
    // Referrals: whoever recruits a trader earns refShareBps of the protocol
    // fee on every trade that trader ever routes, forever.
    // ---------------------------------------------------------------------

    /// @notice Bind your referrer. Once, immutable, no self-referral.
    function setReferrer(address ref) external {
        if (ref == address(0) || ref == msg.sender) revert BadPolicy();
        if (referrerOf[msg.sender] != address(0)) revert AlreadyConfigured();
        referrerOf[msg.sender] = ref;
        emit ReferrerBound(msg.sender, ref);
    }

    // ---------------------------------------------------------------------
    // Configuration (factory-only, once per pool, immutable after)
    // ---------------------------------------------------------------------

    function registerPool(PoolKey calldata key, address coin, address pair, FeePolicy calldata p, bool coinIsCurrency0)
        external
    {
        if (msg.sender != launcher) revert NotLauncher();
        if (p.buyTaxBps > MAX_SIDE_TAX_BPS || p.sellTaxBps > MAX_SIDE_TAX_BPS) revert BadPolicy();
        if (uint256(p.devBps) + p.dividendBps + p.liquidityBps + p.mmBps != BPS) revert BadPolicy();
        if (p.devWallet == address(0)) revert BadPolicy();

        PoolId id = key.toId();
        if (configOf[id].set) revert AlreadyConfigured();
        configOf[id] = Config({
            coin: coin,
            pair: pair,
            policy: p,
            launchTime: uint64(block.timestamp),
            coinIsCurrency0: coinIsCurrency0,
            set: true
        });
        _poolOf[coin] = id;
        emit PoolConfigured(id, coin, p);
    }

    /// @notice The founder can hand the dev-fee slot to another wallet
    ///         (new team wallet, a splitter contract, a DAO…).
    function setDevWallet(address coin, address to) external {
        Config storage c = configOf[_poolOf[coin]];
        if (!c.set || msg.sender != c.policy.devWallet) revert NotDevWallet();
        if (to == address(0)) revert BadPolicy();
        emit DevWalletChanged(coin, c.policy.devWallet, to);
        c.policy.devWallet = to;
    }

    function policyOf(address coin) external view returns (FeePolicy memory, uint64 launchTime) {
        Config storage c = configOf[_poolOf[coin]];
        return (c.policy, c.launchTime);
    }

    // ---------------------------------------------------------------------
    // Fee engine
    // ---------------------------------------------------------------------

    function afterSwap(
        address sender,
        PoolKey calldata key,
        SwapParams calldata params,
        BalanceDelta delta,
        bytes calldata hookData
    ) external onlyPoolManager returns (bytes4, int128) {
        // Internal normalisation swaps (dividends / bid wall) re-enter the
        // pool with this hook as the sender: never tax our own plumbing.
        if (sender == address(this)) return (IHooks.afterSwap.selector, 0);

        PoolId id = key.toId();
        Config memory c = configOf[id];
        if (!c.set) return (IHooks.afterSwap.selector, 0);
        lastSwapAt[id] = uint64(block.timestamp);

        // Direction: a buy takes the coin OUT of the pool.
        bool coinIsOutput = params.zeroForOne != c.coinIsCurrency0;
        uint16 sideTaxBps = coinIsOutput ? c.policy.buyTaxBps : c.policy.sellTaxBps;

        // Sniper premium in the first seconds after graduation; the factory's
        // own swaps (none today) would be exempt by sender check if added.
        uint256 age = block.timestamp - c.launchTime;
        uint16 snipeBps = sender == launcher ? 0 : age < SNIPE_T1 ? SNIPE_BPS_1 : age < SNIPE_T2 ? SNIPE_BPS_2 : 0;

        (Currency feeCurrency, uint256 magnitude) = _unspecified(key, params, delta);
        if (magnitude == 0) return (IHooks.afterSwap.selector, 0);

        // Routers pass the end trader as 32-byte hookData; a trader with a
        // bound referrer gets the referee discount, and the referrer is paid.
        address trader;
        address ref;
        if (hookData.length == 32) {
            trader = abi.decode(hookData, (address));
            ref = referrerOf[trader];
        }
        uint256 platformFee = (magnitude * platformFeeBps) / BPS;
        if (ref != address(0)) platformFee -= (platformFee * REFEREE_DISCOUNT_BPS) / BPS;
        uint256 founderTax = (magnitude * sideTaxBps) / BPS;
        uint256 sniperFee = (magnitude * snipeBps) / BPS;
        uint256 total = platformFee + founderTax + sniperFee;
        if (total == 0) return (IHooks.afterSwap.selector, 0);

        if (platformFee > 0) {
            uint256 toReferrer;
            if (ref != address(0)) {
                toReferrer = (platformFee * refShareBps) / BPS;
                if (toReferrer > 0) {
                    _payOut(feeCurrency, ref, toReferrer);
                    emit ReferralPaid(trader, ref, feeCurrency, toReferrer);
                }
            }
            _payOut(feeCurrency, platformTreasury, platformFee - toReferrer);
        }
        if (founderTax > 0) _settleBuckets(key, id, c, feeCurrency, founderTax, sniperFee);
        else if (sniperFee > 0) _placeQuotes(key, id, c, feeCurrency, sniperFee);

        // Band placement and conversions round in pool-favouring directions,
        // which can strand dust credit on either currency; an unlock only
        // closes when every delta is zero, so sweep whatever remains to the
        // dev wallet before handing control back.
        _sweep(key.currency0, c.policy.devWallet);
        _sweep(key.currency1, c.policy.devWallet);

        emit FeeTaken(id, feeCurrency, coinIsOutput, platformFee, founderTax, sniperFee);
        return (IHooks.afterSwap.selector, total.toInt128());
    }

    function _unspecified(PoolKey calldata key, SwapParams calldata params, BalanceDelta delta)
        private
        pure
        returns (Currency currency, uint256 magnitude)
    {
        int128 amount;
        if (params.amountSpecified < 0) {
            currency = params.zeroForOne ? key.currency1 : key.currency0;
            amount = params.zeroForOne ? delta.amount1() : delta.amount0();
        } else {
            currency = params.zeroForOne ? key.currency0 : key.currency1;
            amount = params.zeroForOne ? delta.amount0() : delta.amount1();
        }
        magnitude = amount < 0 ? uint256(uint128(-amount)) : uint256(uint128(amount));
    }

    /// @dev Split the founder tax across the four buckets; the sniper premium
    ///      rides along and lands entirely in the bid wall.
    function _settleBuckets(
        PoolKey calldata key,
        PoolId id,
        Config memory c,
        Currency feeCurrency,
        uint256 founderTax,
        uint256 sniperFee
    ) private {
        uint256 toDev = (founderTax * c.policy.devBps) / BPS;
        uint256 toDividends = (founderTax * c.policy.dividendBps) / BPS;
        uint256 toLiquidity = (founderTax * c.policy.liquidityBps) / BPS;
        uint256 toWall = founderTax - toDev - toDividends - toLiquidity + sniperFee;

        if (toDev > 0) _payOut(feeCurrency, c.policy.devWallet, toDev);

        uint256 dividendsPaid;
        if (toDividends > 0) dividendsPaid = _payDividends(key, c, feeCurrency, toDividends);

        uint256 liquidityAdded;
        if (toLiquidity > 0) liquidityAdded = _addBand(key, id, feeCurrency, toLiquidity, LP_WIDTH, false);

        uint256 wallPlaced;
        if (toWall > 0) wallPlaced = _placeQuotes(key, id, c, feeCurrency, toWall);

        // Whatever a bucket could not place (band out of range, failed
        // conversion) goes to the dev wallet rather than getting stuck.
        uint256 leftovers = (toDividends - dividendsPaid) + (toLiquidity - liquidityAdded) + (toWall - wallPlaced);
        if (leftovers > 0) _payOut(feeCurrency, c.policy.devWallet, leftovers);

        emit BucketsSettled(id, toDev + leftovers, dividendsPaid, liquidityAdded, wallPlaced);
    }

    /// @dev Dividends are always paid in the pair token: convert coin-side
    ///      fees through the pool, then credit QuiverToken's tracker.
    function _payDividends(PoolKey calldata key, Config memory c, Currency feeCurrency, uint256 amount)
        private
        returns (uint256 used)
    {
        bool feeIsCoin = Currency.unwrap(feeCurrency) == c.coin;
        uint256 pairAmount = amount;
        if (feeIsCoin) {
            (uint256 spent, uint256 got) = _convert(key, feeCurrency, amount);
            if (got == 0) return 0;
            used = spent;
            pairAmount = got;
        } else {
            used = amount;
        }
        Currency pairCurrency = c.coinIsCurrency0 ? key.currency1 : key.currency0;
        // Plain ERC-20 push to the token contract, then credit the tracker.
        poolManager.take(pairCurrency, c.coin, pairAmount);
        IQuiverToken(c.coin).distributeRewards(pairAmount);
    }

    /// @dev Two-sided quoting: half the market-making amount is posted as a
    ///      tight band in its own currency (coin fees refill the ask above the
    ///      price, pair fees the bid below), the other half is converted and
    ///      posted on the opposite side — so every trade tightens the spread
    ///      from both directions.
    function _placeQuotes(PoolKey memory key, PoolId id, Config memory c, Currency feeCurrency, uint256 amount)
        private
        returns (uint256 used)
    {
        if (amount == 0) return 0;
        Currency other = Currency.unwrap(feeCurrency) == Currency.unwrap(key.currency0) ? key.currency1 : key.currency0;

        uint256 direct = amount / 2;
        uint256 toFlip = amount - direct;

        used = _addBand(key, id, feeCurrency, direct, WALL_WIDTH, true);

        (uint256 spent, uint256 got) = _convert(key, feeCurrency, toFlip);
        used += spent;
        if (got > 0) {
            uint256 placed = _addBand(key, id, other, got, WALL_WIDTH, true);
            if (placed < got) {
                // Unplaceable remainder (price at the edge): pool credit to
                // the dev wallet rather than reverting the trade.
                _payOut(other, c.policy.devWallet, got - placed);
            }
        }
    }

    /// @dev Sell `amount` of `currencyIn` through the pool for the other side.
    ///      Self-swaps skip the fee logic via the sender guard in afterSwap.
    function _convert(PoolKey memory key, Currency currencyIn, uint256 amount)
        private
        returns (uint256 spent, uint256 got)
    {
        bool zeroForOne = Currency.unwrap(currencyIn) == Currency.unwrap(key.currency0);
        try poolManager.swap(
            key,
            SwapParams({
                zeroForOne: zeroForOne,
                amountSpecified: -int256(amount),
                sqrtPriceLimitX96: zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            ""
        ) returns (BalanceDelta d) {
            int128 inDelta = zeroForOne ? d.amount0() : d.amount1();
            int128 outDelta = zeroForOne ? d.amount1() : d.amount0();
            if (outDelta <= 0 || inDelta >= 0) return (0, 0);
            spent = uint256(uint128(-inDelta));
            got = uint256(uint128(outDelta));
        } catch {
            return (0, 0);
        }
    }

    function _addBand(PoolKey memory key, PoolId id, Currency currency, uint256 amount, int24 widthSpacings, bool wall)
        private
        returns (uint256 spent)
    {
        (, int24 tick,,) = poolManager.getSlot0(id);
        int24 spacing = key.tickSpacing;
        bool isToken0 = Currency.unwrap(currency) == Currency.unwrap(key.currency0);

        int24 aligned = (tick / spacing) * spacing;
        int24 lower;
        int24 upper;
        if (isToken0) {
            lower = aligned + spacing;
            upper = lower + spacing * widthSpacings;
            if (upper >= TickMath.MAX_TICK) return 0;
        } else {
            upper = aligned - spacing;
            lower = upper - spacing * widthSpacings;
            if (lower <= TickMath.MIN_TICK) return 0;
        }

        uint128 liquidity = isToken0
            ? LiquidityAmounts.getLiquidityForAmount0(TickMath.getSqrtPriceAtTick(lower), TickMath.getSqrtPriceAtTick(upper), amount)
            : LiquidityAmounts.getLiquidityForAmount1(TickMath.getSqrtPriceAtTick(lower), TickMath.getSqrtPriceAtTick(upper), amount);
        if (liquidity == 0) return 0;

        try poolManager.modifyLiquidity(
            key,
            ModifyLiquidityParams({tickLower: lower, tickUpper: upper, liquidityDelta: int256(uint256(liquidity)), salt: bytes32(0)}),
            ""
        ) returns (BalanceDelta callerDelta, BalanceDelta) {
            int128 cost = isToken0 ? callerDelta.amount0() : callerDelta.amount1();
            spent = cost < 0 ? uint256(uint128(-cost)) : 0;
            emit LiquidityAdded(id, currency, spent, liquidity, wall, lower, upper);
        } catch {
            spent = 0;
        }
    }

    function _sweep(Currency currency, address to) private {
        // Inlined TransientStateLibrary.currencyDelta (that library's imports
        // need a Cancun compile target; exttload is just an external call).
        bytes32 slot;
        address self = address(this);
        assembly ("memory-safe") {
            mstore(0, and(self, 0xffffffffffffffffffffffffffffffffffffffff))
            mstore(32, and(currency, 0xffffffffffffffffffffffffffffffffffffffff))
            slot := keccak256(0, 64)
        }
        int256 d = int256(uint256(poolManager.exttload(slot)));
        if (d > 0) _payOut(currency, to, uint256(d));
    }

    function _payOut(Currency currency, address to, uint256 amount) private {
        if (amount == 0) return;
        try poolManager.take{gas: PAYOUT_GAS}(currency, to, amount) {
            return;
        } catch {
            poolManager.mint(to, currency.toId(), amount);
            emit PayoutDeferred(to, currency, amount);
        }
    }

    // ---------------------------------------------------------------------
    // Wall re-centering: anyone can migrate the hook's stale quote walls back
    // beside the current price. The caller supplies the live band set
    // (reconstructed off-chain from LiquidityAdded/WallRemoved events); a
    // wrong list simply reverts. Only WALL_WIDTH bands qualify, so the
    // permanent auto-liquidity book can never be churned through this path.
    // ---------------------------------------------------------------------

    /// @notice Current pool tick for a registered coin (keeper convenience).
    function poolTick(address coin) external view returns (int24 tick) {
        PoolId id = _poolOf[coin];
        if (!configOf[id].set) revert BadPolicy();
        (, tick,,) = poolManager.getSlot0(id);
    }

    function recenter(address coin, Band[] calldata bands) external nonReentrant {
        PoolId id = _poolOf[coin];
        Config storage c = configOf[id];
        if (!c.set) revert BadPolicy();
        if (block.timestamp < uint256(lastSwapAt[id]) + RECENTER_QUIET_SECS) revert PriceMoving();
        if (bands.length == 0) revert BadPolicy();
        poolManager.unlock(abi.encode(coin, bands));
    }

    function unlockCallback(bytes calldata data) external onlyPoolManager returns (bytes memory) {
        (address coin, Band[] memory bands) = abi.decode(data, (address, Band[]));
        Config memory c = configOf[_poolOf[coin]];

        bool c0 = c.coinIsCurrency0;
        PoolKey memory key = PoolKey({
            currency0: Currency.wrap(c0 ? c.coin : c.pair),
            currency1: Currency.wrap(c0 ? c.pair : c.coin),
            fee: LP_FEE,
            tickSpacing: TICK_SPACING,
            hooks: IHooks(address(this))
        });
        PoolId id = key.toId();

        // 1) Pull every supplied wall band back into pool credit.
        uint256 amt0;
        uint256 amt1;
        for (uint256 i = 0; i < bands.length; i++) {
            Band memory b = bands[i];
            if (b.upper - b.lower != WALL_WIDTH * TICK_SPACING) revert BadPolicy();
            (BalanceDelta d,) = poolManager.modifyLiquidity(
                key,
                ModifyLiquidityParams({
                    tickLower: b.lower,
                    tickUpper: b.upper,
                    liquidityDelta: -int256(uint256(b.liquidity)),
                    salt: bytes32(0)
                }),
                ""
            );
            if (d.amount0() > 0) amt0 += uint256(uint128(d.amount0()));
            if (d.amount1() > 0) amt1 += uint256(uint128(d.amount1()));
            emit WallRemoved(id, b.lower, b.upper, b.liquidity);
        }

        // 2) Re-post everything as fresh tight quotes beside the current
        //    price: currency0 credit on the ask side of its book, currency1
        //    on the bid side (each _addBand picks the correct side itself).
        uint256 placed0 = amt0 > 0 ? _addBand(key, id, key.currency0, amt0, WALL_WIDTH, true) : 0;
        uint256 placed1 = amt1 > 0 ? _addBand(key, id, key.currency1, amt1, WALL_WIDTH, true) : 0;
        if (amt0 > placed0) _payOut(key.currency0, c.policy.devWallet, amt0 - placed0);
        if (amt1 > placed1) _payOut(key.currency1, c.policy.devWallet, amt1 - placed1);
        _sweep(key.currency0, c.policy.devWallet);
        _sweep(key.currency1, c.policy.devWallet);

        emit WallRecentered(id, bands.length, c0 ? placed1 : placed0, c0 ? placed0 : placed1);
        return "";
    }

    // ---------------------------------------------------------------------
    // IHooks surface — only beforeInitialize + afterSwap(+returnDelta) live;
    // the rest revert and are never reached (address flags gate the calls).
    // ---------------------------------------------------------------------

    function beforeInitialize(address sender, PoolKey calldata, uint160) external view returns (bytes4) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        if (sender != launcher) revert NotLauncher();
        return IHooks.beforeInitialize.selector;
    }

    function afterInitialize(address, PoolKey calldata, uint160, int24) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function beforeAddLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata)
        external
        pure
        returns (bytes4)
    {
        revert HookNotImplemented();
    }

    function afterAddLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external pure returns (bytes4, BalanceDelta) {
        revert HookNotImplemented();
    }

    function beforeRemoveLiquidity(address, PoolKey calldata, ModifyLiquidityParams calldata, bytes calldata)
        external
        pure
        returns (bytes4)
    {
        revert HookNotImplemented();
    }

    function afterRemoveLiquidity(
        address,
        PoolKey calldata,
        ModifyLiquidityParams calldata,
        BalanceDelta,
        BalanceDelta,
        bytes calldata
    ) external pure returns (bytes4, BalanceDelta) {
        revert HookNotImplemented();
    }

    function beforeSwap(address, PoolKey calldata, SwapParams calldata, bytes calldata)
        external
        pure
        returns (bytes4, BeforeSwapDelta, uint24)
    {
        revert HookNotImplemented();
    }

    function beforeDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }

    function afterDonate(address, PoolKey calldata, uint256, uint256, bytes calldata) external pure returns (bytes4) {
        revert HookNotImplemented();
    }
}
