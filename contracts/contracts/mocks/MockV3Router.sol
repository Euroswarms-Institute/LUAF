// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice Test double for the V3 SwapRouter surface the routers use. It pulls
///         the input, then pays the output at a fixed rate from its own
///         balance, so a test can exercise the routing and fee logic without a
///         real pool. Pre-fund it with the output token.
contract MockV3Router {
    /// @dev Output per unit of input, scaled by 1e18.
    uint256 public rateQ18 = 1e18;

    function setRate(uint256 rateQ18_) external {
        rateQ18 = rateQ18_;
    }

    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }

    function exactInput(ExactInputParams calldata p) external payable returns (uint256 amountOut) {
        address tokenIn = address(bytes20(p.path[:20]));
        address tokenOut = address(bytes20(p.path[p.path.length - 20:]));
        IERC20(tokenIn).transferFrom(msg.sender, address(this), p.amountIn);
        amountOut = (p.amountIn * rateQ18) / 1e18;
        require(amountOut >= p.amountOutMinimum, "mock slippage");
        IERC20(tokenOut).transfer(p.recipient, amountOut);
    }
}
