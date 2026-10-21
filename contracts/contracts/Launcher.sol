// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

import {VentureFactory} from "./vendor/doubleplus/v4/venture/VentureFactory.sol";

/// @title Launcher
/// @notice The creator of every coin the pipeline launches into one tenant's
///         venture factory. Being the creator is what earns the creator side
///         of a coin: the Open-mode creator curve share (factory fee ledger)
///         and the founder tax's dev bucket (paid by the hook to devWallet).
///         Both are routed to `splitter`, a DoublePlus FeeSplitter deployed
///         tenant / protocol, so the creator split is enforced by where the
///         income is addressed, not by an agreement.
///
///         `launch` is operator-only: one transaction launches the coin and,
///         optionally, makes the operator's first buy inside the launch
///         window. The bought coins and any change go to the operator; they
///         are the operator's capital, not creator income.
///
///         Each launch is tagged with a `specId` so off-chain selection can
///         join a coin's fee income back to the launch spec that produced it.
contract Launcher is ReentrancyGuard {
    using SafeERC20 for IERC20;

    address public immutable operator;
    address payable public immutable splitter;

    event Launched(bytes32 indexed specId, address indexed token, address indexed factory, uint256 firstBuyWei, uint256 tokensOut);
    event Forwarded(address indexed factory, uint256 amount);

    error NotOperator();
    error BadConfig();
    error BadParams();
    error EthTransferFailed();

    modifier onlyOperator() {
        if (msg.sender != operator) revert NotOperator();
        _;
    }

    constructor(address operator_, address payable splitter_) {
        if (operator_ == address(0) || splitter_ == address(0)) revert BadConfig();
        operator = operator_;
        splitter = splitter_;
    }

    /// @dev Receives change from the first buy and the factory's fee ledger.
    receive() external payable {}

    /// @notice Launch a coin with this contract as creator, then buy into it.
    /// @param p             launch parameters. devWallet must be the splitter
    ///                      (the dev bucket is creator income) and the founder
    ///                      supply must be zero (vested coins would sit here).
    ///         msg.value is creationFeeWei plus the first buy, which must fit
    ///         the factory's launch-window cap or the whole launch reverts.
    function launch(
        VentureFactory factory,
        VentureFactory.LaunchParams calldata p,
        bytes32 salt,
        bytes32 specId,
        uint256 minTokensOut
    ) external payable onlyOperator nonReentrant returns (address token, uint256 tokensOut) {
        if (p.devWallet != splitter || p.founderSupplyBps != 0) revert BadParams();
        uint256 fee = factory.creationFeeWei();
        if (msg.value < fee) revert BadParams();
        uint256 base = address(this).balance - msg.value;

        token = factory.launch{value: fee}(p, salt);
        uint256 firstBuy = msg.value - fee;
        if (firstBuy > 0) {
            tokensOut = factory.buy{value: firstBuy}(token, minTokensOut);
            IERC20(token).safeTransfer(operator, tokensOut);
        }

        // buy() refunds what the curve could not take; that is the operator's.
        uint256 change = address(this).balance - base;
        if (change > 0) _sendEth(operator, change);
        emit Launched(specId, token, address(factory), firstBuy, tokensOut);
    }

    /// @notice Move creator fees from a factory's ledger to the splitter.
    ///         Anyone may call: the ETH can only go to the splitter.
    function pull(VentureFactory factory) external nonReentrant returns (uint256 amount) {
        if (factory.feesAccrued(address(this)) > 0) factory.withdrawFees();
        amount = address(this).balance;
        if (amount > 0) {
            _sendEth(splitter, amount);
            emit Forwarded(address(factory), amount);
        }
    }

    function _sendEth(address to, uint256 amount) private {
        (bool ok,) = payable(to).call{value: amount}("");
        if (!ok) revert EthTransferFailed();
    }
}
