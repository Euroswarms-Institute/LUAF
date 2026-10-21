// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {IHooks} from "@uniswap/v4-core/src/interfaces/IHooks.sol";
import {PoolKey} from "@uniswap/v4-core/src/types/PoolKey.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";
import {BalanceDelta} from "@uniswap/v4-core/src/types/BalanceDelta.sol";
import {SwapParams} from "@uniswap/v4-core/src/types/PoolOperation.sol";
import {TickMath} from "@uniswap/v4-core/src/libraries/TickMath.sol";

import {VentureFactory} from "./VentureFactory.sol";

interface IWrappedNative {
    function deposit() external payable;
    function withdraw(uint256) external;
}

/// @notice Minimal Uniswap V3 SwapRouter surface (SwapRouter02-style, no
///         deadline). Used for the ETH<->pair leg, which lives in the canonical
///         V3 pools (WETH/USDG and USDG/stock).
interface ISwapRouterV3 {
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata params) external payable returns (uint256 amountOut);
}

/// @title VentureRouter
/// @notice One-tap buy/sell for graduated ventures. Same shape as RhRouter,
///         with two additions that power the growth loop:
///
///           - every V4 swap carries the end trader as hookData, so the fee
///             hook can pay that trader's referrer their cut of the protocol
///             fee in the same transaction;
///           - every routed trade emits Routed(trader, coin, ...), the data
///             feed the weekly jackpot keeper ranks traders by.
///
///         A buyer holds only native ETH, so the router bridges the gap:
///
///           buy:  ETH -> WETH -[V3 path]-> pair -[our V4 pool]-> coin
///           sell: coin -[our V4 pool]-> pair -[V3 path]-> WETH -> ETH
///
///         The V3 path (WETH -> USDG -> stock, or WETH -> meme) is supplied by
///         the caller/frontend from the discovered route map, so the router
///         needs no per-pair config. When the pair IS WETH, the V3 leg is
///         skipped. The pair->coin (and coin->pair) leg always runs through the
///         launched coin's own V4 pool via the PoolManager unlock.
contract VentureRouter is IUnlockCallback, ReentrancyGuard {
    using SafeERC20 for IERC20;

    int24 internal constant TICK_SPACING = 60;
    uint24 internal constant LP_FEE = 0;

    IPoolManager public immutable poolManager;
    VentureFactory public immutable factory;
    IHooks public immutable hook;
    address public immutable weth;
    ISwapRouterV3 public immutable v3Router;

    struct V4Swap {
        PoolKey key;
        bool zeroForOne;
        uint256 amountIn;
        address trader;
    }

    error NotListed();
    error Slippage();

    event Routed(address indexed trader, address indexed coin, bool isBuy, uint256 ethIn, uint256 ethOut);

    constructor(IPoolManager pm, VentureFactory factory_, address weth_, ISwapRouterV3 v3Router_) {
        poolManager = pm;
        factory = factory_;
        hook = IHooks(address(factory_.hook()));
        weth = weth_;
        v3Router = v3Router_;
    }

    receive() external payable {}

    // ---------------------------------------------------------------------
    // Buy: ETH -> pair -> coin
    // ---------------------------------------------------------------------

    /// @param coin     the launched coin to buy.
    /// @param v3Path   encoded Uniswap V3 path WETH -> ... -> pair. Empty when
    ///                 the pair token IS WETH (no V3 leg needed).
    /// @param minCoinOut slippage floor on the coin received.
    function buy(address coin, bytes calldata v3Path, uint256 minCoinOut)
        external
        payable
        nonReentrant
        returns (uint256 coinOut)
    {
        address pair = _pairOf(coin);
        require(msg.value > 0, "no ETH");

        // 1. Wrap ETH.
        IWrappedNative(weth).deposit{value: msg.value}();

        // 2. WETH -> pair via V3 (skip when the pair is WETH itself).
        uint256 pairAmount;
        if (pair == weth) {
            pairAmount = msg.value;
        } else {
            IERC20(weth).forceApprove(address(v3Router), msg.value);
            pairAmount = v3Router.exactInput(
                ISwapRouterV3.ExactInputParams({path: v3Path, recipient: address(this), amountIn: msg.value, amountOutMinimum: 0})
            );
        }

        // 3. pair -> coin via the coin's own V4 pool.
        coinOut = _v4Swap(coin, pair, pair, pairAmount);
        if (coinOut < minCoinOut) revert Slippage();
        IERC20(coin).safeTransfer(msg.sender, coinOut);
        emit Routed(msg.sender, coin, true, msg.value, 0);
    }

    // ---------------------------------------------------------------------
    // Sell: coin -> pair -> ETH
    // ---------------------------------------------------------------------

    /// @param v3PathReverse encoded V3 path pair -> ... -> WETH. Empty when the
    ///                      pair token IS WETH.
    function sell(address coin, uint256 amountIn, bytes calldata v3PathReverse, uint256 minEthOut)
        external
        nonReentrant
        returns (uint256 ethOut)
    {
        address pair = _pairOf(coin);
        IERC20(coin).safeTransferFrom(msg.sender, address(this), amountIn);

        // 1. coin -> pair via the coin's own V4 pool.
        uint256 pairAmount = _v4Swap(coin, pair, coin, amountIn);

        // 2. pair -> WETH via V3 (skip when the pair is WETH itself).
        uint256 wethAmount;
        if (pair == weth) {
            wethAmount = pairAmount;
        } else {
            IERC20(pair).forceApprove(address(v3Router), pairAmount);
            wethAmount = v3Router.exactInput(
                ISwapRouterV3.ExactInputParams({path: v3PathReverse, recipient: address(this), amountIn: pairAmount, amountOutMinimum: 0})
            );
        }

        // 3. Unwrap and pay out native ETH.
        IWrappedNative(weth).withdraw(wethAmount);
        ethOut = wethAmount;
        if (ethOut < minEthOut) revert Slippage();
        (bool ok, ) = msg.sender.call{value: ethOut}("");
        require(ok, "eth xfer");
        emit Routed(msg.sender, coin, false, 0, ethOut);
    }

    // ---------------------------------------------------------------------
    // V4 swap plumbing (coin <-> pair through the launched coin's pool)
    // ---------------------------------------------------------------------

    function _pairOf(address coin) internal view returns (address pair) {
        (, pair, , , ) = factory.listings(coin);
        if (pair == address(0)) revert NotListed();
    }

    function _poolKey(address coin, address pair) internal view returns (PoolKey memory key) {
        bool coinIsC0 = coin < pair;
        key = PoolKey({
            currency0: Currency.wrap(coinIsC0 ? coin : pair),
            currency1: Currency.wrap(coinIsC0 ? pair : coin),
            fee: LP_FEE,
            tickSpacing: TICK_SPACING,
            hooks: hook
        });
    }

    /// @dev Swap `amountIn` of `currencyIn` for the other side of the coin/pair
    ///      pool. `currencyIn` is either the coin (sell) or the pair (buy).
    function _v4Swap(address coin, address pair, address currencyIn, uint256 amountIn)
        internal
        returns (uint256 amountOut)
    {
        if (amountIn == 0) return 0;
        PoolKey memory key = _poolKey(coin, pair);
        bool zeroForOne = currencyIn == Currency.unwrap(key.currency0);
        bytes memory res = poolManager.unlock(abi.encode(V4Swap(key, zeroForOne, amountIn, msg.sender)));
        amountOut = abi.decode(res, (uint256));
    }

    function unlockCallback(bytes calldata data) external override returns (bytes memory) {
        require(msg.sender == address(poolManager), "not pool manager");
        V4Swap memory a = abi.decode(data, (V4Swap));

        // The trader rides as hookData so the fee hook can attribute the
        // trade for referral payouts.
        BalanceDelta delta = poolManager.swap(
            a.key,
            SwapParams({
                zeroForOne: a.zeroForOne,
                amountSpecified: -int256(a.amountIn),
                sqrtPriceLimitX96: a.zeroForOne ? TickMath.MIN_SQRT_PRICE + 1 : TickMath.MAX_SQRT_PRICE - 1
            }),
            abi.encode(a.trader)
        );

        _resolve(a.key.currency0, delta.amount0());
        _resolve(a.key.currency1, delta.amount1());

        uint256 out = a.zeroForOne ? uint256(uint128(delta.amount1())) : uint256(uint128(delta.amount0()));
        return abi.encode(out);
    }

    function _resolve(Currency currency, int128 amount) internal {
        if (amount < 0) {
            uint256 owed = uint256(uint128(-amount));
            poolManager.sync(currency);
            IERC20(Currency.unwrap(currency)).safeTransfer(address(poolManager), owed);
            poolManager.settle();
        } else if (amount > 0) {
            poolManager.take(currency, address(this), uint256(uint128(amount)));
        }
    }
}
