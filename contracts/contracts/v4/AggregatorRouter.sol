// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

import {SyndicateRegistry} from "./SyndicateRegistry.sol";

interface IWrappedNative {
    function deposit() external payable;
    function withdraw(uint256) external;
}

interface ISwapRouterV3 {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

/// @title AggregatorRouter
/// @notice Routes a trade through any pool — ours or anyone else's — and takes
///         a fee on the way in.
///
///         VentureRouter only serves coins our own factory launched: it reads
///         the pair from `factory.listings` and refuses anything else. That is
///         correct for a launchpad and useless for a terminal, where the point
///         is to trade every coin on the chain regardless of who launched it.
///         Here the caller supplies the pool itself, so a pool needs no
///         registration and a launchpad we have never heard of is tradeable the
///         day it ships.
///
///         The fee is taken from the **input** amount, before the swap. That
///         makes it knowable before signing, independent of the swap's result,
///         and impossible to dodge with a slippage game. It arrives in whatever
///         the caller spent, so the treasury accumulates many assets; the
///         treasury keeper already sells those down to ETH.
///
///         Nothing here is upgradeable and nothing is withdrawable by an admin:
///         the fee goes straight to an immutable treasury, and the contract
///         never holds a user's funds beyond the transaction.
contract AggregatorRouter is IUnlockCallback, ReentrancyGuard {
    using SafeERC20 for IERC20;

    /// @notice The hard ceiling on the fee, set at 1% to match what terminals
    ///         charge. Immutable, so the deployed rate can never exceed it.
    uint16 public constant MAX_FEE_BPS = 100;
    uint16 internal constant BPS = 10_000;

    IPoolManager public immutable poolManager;
    ISwapRouterV3 public immutable v3Router;
    address public immutable weth;
    address public immutable treasury;
    uint16 public immutable feeBps;

    /// @notice The syndicate's membership roll, or the zero address for no
    ///         syndicate at all. See SyndicateRegistry: this router is the
    ///         "central sales agency" that makes leaving cost something.
    SyndicateRegistry public immutable registry;
    /// @notice The share of this router's fee paid to a member whose coin the
    ///         trade touched, in basis points of the fee.
    uint16 public immutable rebateBps;

    /// @notice Rebates credited but not yet taken, by beneficiary and token.
    ///         Credited rather than pushed: a member treasury that reverts or
    ///         burns gas must never be able to fail a stranger's trade.
    mapping(address beneficiary => mapping(address token => uint256)) public owed;

    struct V4Swap {
        PoolKey key;
        bool zeroForOne;
        uint256 amountIn;
        bytes hookData;
    }

    event Routed(
        address indexed trader,
        address indexed tokenIn,
        address indexed tokenOut,
        uint256 amountIn,
        uint256 amountOut,
        uint256 feeAmount
    );
    event RebateAccrued(address indexed beneficiary, address indexed token, address indexed coin, uint256 amount);
    event RebateClaimed(address indexed beneficiary, address indexed token, uint256 amount);

    error BadConfig();
    error Slippage();
    error NoInput();
    error NothingOwed();
    error NotPoolManager();
    error EthTransferFailed();
    error WrongInputForEth();

    constructor(
        IPoolManager pm,
        ISwapRouterV3 v3Router_,
        address weth_,
        address treasury_,
        uint16 feeBps_,
        SyndicateRegistry registry_,
        uint16 rebateBps_
    ) {
        if (weth_ == address(0) || treasury_ == address(0) || feeBps_ > MAX_FEE_BPS) revert BadConfig();
        // A rebate above the whole fee would pay out money the router never
        // took. Note a rebate with no registry is simply never reachable.
        if (rebateBps_ > BPS) revert BadConfig();
        poolManager = pm;
        v3Router = v3Router_;
        weth = weth_;
        treasury = treasury_;
        feeBps = feeBps_;
        registry = registry_;
        rebateBps = rebateBps_;
    }

    /// @dev Only the WETH contract pays this, on withdraw().
    receive() external payable {}

    /// @notice The fee on a given input, so a caller can quote itself.
    function quoteFee(uint256 amountIn) external view returns (uint256) {
        return (amountIn * feeBps) / BPS;
    }

    // ---------------------------------------------------------------------
    // Taking the input: either native ETH (wrapped here) or an ERC-20
    // ---------------------------------------------------------------------

    /// @dev Pulls `amountIn` of `tokenIn` in and works out the fee, leaving it
    ///      held here for `_settleFee` to divide once the caller knows both
    ///      sides of the trade. With native ETH the caller sends value and
    ///      names WETH as the input.
    function _takeInput(address tokenIn, uint256 amountIn) private returns (uint256 netIn, uint256 fee) {
        if (msg.value > 0) {
            if (tokenIn != weth) revert WrongInputForEth();
            amountIn = msg.value;
            IWrappedNative(weth).deposit{value: msg.value}();
        } else {
            if (amountIn == 0) revert NoInput();
            IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        }
        if (amountIn == 0) revert NoInput();
        fee = (amountIn * feeBps) / BPS;
        netIn = amountIn - fee;
        if (netIn == 0) revert NoInput();
    }

    /// @dev Divide the fee: a share to the syndicate member whose coin this
    ///      trade touched, the rest to the treasury.
    ///
    ///      Which coin counts is read from the pool, never from an argument.
    ///      A caller who could name the coin would simply name a member coin
    ///      on every unrelated trade and collect the rebate. The output side
    ///      is checked first and then the input, so a buy and a sell of the
    ///      same coin both pay its launchpad.
    function _settleFee(address tokenIn, uint256 fee, address tokenOut) private {
        if (fee == 0) return;
        uint256 toTreasury = fee;
        if (address(registry) != address(0) && rebateBps > 0) {
            address coin = tokenOut;
            address beneficiary = registry.beneficiaryFor(coin);
            if (beneficiary == address(0)) {
                coin = tokenIn;
                beneficiary = registry.beneficiaryFor(coin);
            }
            if (beneficiary != address(0)) {
                uint256 rebate = (fee * rebateBps) / BPS;
                if (rebate > 0) {
                    owed[beneficiary][tokenIn] += rebate;
                    toTreasury = fee - rebate;
                    emit RebateAccrued(beneficiary, tokenIn, coin, rebate);
                }
            }
        }
        if (toTreasury > 0) IERC20(tokenIn).safeTransfer(treasury, toTreasury);
    }

    /// @notice Take what the syndicate owes you. Pull, not push: a trade must
    ///         never fail because a member's treasury cannot receive.
    function claimRebate(address token) external nonReentrant returns (uint256 amount) {
        amount = owed[msg.sender][token];
        if (amount == 0) revert NothingOwed();
        owed[msg.sender][token] = 0;
        IERC20(token).safeTransfer(msg.sender, amount);
        emit RebateClaimed(msg.sender, token, amount);
    }

    /// @dev Hands the proceeds to the recipient, unwrapping to native ETH when
    ///      asked and the output really is WETH.
    function _payOut(address tokenOut, uint256 amount, address recipient, bool unwrap) private {
        address to = recipient == address(0) ? msg.sender : recipient;
        if (unwrap && tokenOut == weth) {
            IWrappedNative(weth).withdraw(amount);
            (bool ok,) = payable(to).call{value: amount}("");
            if (!ok) revert EthTransferFailed();
        } else {
            IERC20(tokenOut).safeTransfer(to, amount);
        }
    }

    // ---------------------------------------------------------------------
    // Uniswap V4: the caller names the pool, so any pool works
    // ---------------------------------------------------------------------

    /// @notice Swap through one V4 pool, exact input.
    /// @param key        the pool, supplied by the caller — including its hook.
    /// @param zeroForOne direction: true spends currency0.
    /// @param amountIn   input amount. Ignored when native ETH is sent.
    /// @param minOut     slippage floor, checked after the fee.
    /// @param recipient  who receives the output; 0 means the caller.
    /// @param unwrapOut  unwrap WETH output to native ETH.
    /// @param hookData   passed through to the pool's hook untouched. Our own
    ///                   fee hook reads the trader from it to pay referrals, so
    ///                   a trade routed here earns a referrer exactly as one
    ///                   routed through VentureRouter does.
    function swapV4(
        PoolKey calldata key,
        bool zeroForOne,
        uint256 amountIn,
        uint256 minOut,
        address recipient,
        bool unwrapOut,
        bytes calldata hookData
    ) external payable nonReentrant returns (uint256 amountOut) {
        address tokenIn = Currency.unwrap(zeroForOne ? key.currency0 : key.currency1);
        address tokenOut = Currency.unwrap(zeroForOne ? key.currency1 : key.currency0);
        (uint256 netIn, uint256 fee) = _takeInput(tokenIn, amountIn);
        _settleFee(tokenIn, fee, tokenOut);

        bytes memory res = poolManager.unlock(abi.encode(V4Swap(key, zeroForOne, netIn, hookData)));
        amountOut = abi.decode(res, (uint256));
        if (amountOut < minOut) revert Slippage();

        _payOut(tokenOut, amountOut, recipient, unwrapOut);
        emit Routed(msg.sender, tokenIn, tokenOut, netIn + fee, amountOut, fee);
    }

    function unlockCallback(bytes calldata data) external override returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        V4Swap memory a = abi.decode(data, (V4Swap));

        BalanceDelta delta = poolManager.swap(
            a.key,
            SwapParams({
                zeroForOne: a.zeroForOne,
                amountSpecified: -int256(a.amountIn),
                sqrtPriceLimitX96: a.zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            a.hookData
        );

        _resolve(a.key.currency0, delta.amount0());
        _resolve(a.key.currency1, delta.amount1());

        uint256 out = a.zeroForOne ? uint256(uint128(delta.amount1())) : uint256(uint128(delta.amount0()));
        return abi.encode(out);
    }

    function _resolve(Currency currency, int128 amount) private {
        if (amount < 0) {
            uint256 due = uint256(uint128(-amount));
            poolManager.sync(currency);
            IERC20(Currency.unwrap(currency)).safeTransfer(address(poolManager), due);
            poolManager.settle();
        } else if (amount > 0) {
            poolManager.take(currency, address(this), uint256(uint128(amount)));
        }
    }

    // ---------------------------------------------------------------------
    // Uniswap V3: for launchpads that graduate into V3 pools
    // ---------------------------------------------------------------------

    /// @notice Swap along a V3 path, exact input. `path` is the usual encoded
    ///         route and its first 20 bytes name the input token.
    function swapV3(
        bytes calldata path,
        uint256 amountIn,
        uint256 minOut,
        address recipient,
        bool unwrapOut
    ) external payable nonReentrant returns (uint256 amountOut) {
        if (path.length < 43) revert BadConfig(); // token + fee + token
        address tokenIn = address(bytes20(path[:20]));
        address tokenOut = address(bytes20(path[path.length - 20:]));
        (uint256 netIn, uint256 fee) = _takeInput(tokenIn, amountIn);
        _settleFee(tokenIn, fee, tokenOut);

        IERC20(tokenIn).forceApprove(address(v3Router), netIn);
        // The router pays this contract, not the recipient, so the output can
        // be unwrapped and the slippage floor checked here rather than trusted.
        amountOut = v3Router.exactInput(
            ISwapRouterV3.ExactInputParams({path: path, recipient: address(this), amountIn: netIn, amountOutMinimum: 0})
        );
        if (amountOut < minOut) revert Slippage();

        _payOut(tokenOut, amountOut, recipient, unwrapOut);
        emit Routed(msg.sender, tokenIn, tokenOut, netIn + fee, amountOut, fee);
    }
}
