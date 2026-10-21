// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title FounderVesting
/// @notice Per-launch linear vesting escrow for the founder's token allocation.
///         The factory funds it at launch and arms the clock at graduation, so
///         nothing unlocks while the raise is still live. If the raise is
///         aborted the factory reclaims the balance (and burns it) before the
///         clock ever starts. Once started, the escrow is fully autonomous:
///         only the beneficiary can pull, strictly along the linear schedule.
contract FounderVesting {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;
    address public immutable beneficiary;
    address public immutable factory;
    uint64 public immutable duration; // seconds

    uint64 public startTime; // 0 until the raise graduates
    uint256 public totalAllocation; // snapshotted at start()
    uint256 public released;

    event VestingStarted(uint64 startTime, uint256 allocation);
    event Claimed(uint256 amount);
    event Reclaimed(uint256 amount);

    error OnlyFactory();
    error OnlyBeneficiary();
    error NotStarted();
    error AlreadyStarted();
    error NothingToClaim();

    constructor(address token_, address beneficiary_, uint64 duration_, address factory_) {
        require(token_ != address(0) && beneficiary_ != address(0) && duration_ > 0 && factory_ != address(0), "bad params");
        factory = factory_;
        token = IERC20(token_);
        beneficiary = beneficiary_;
        duration = duration_;
    }

    /// @notice Arm the clock at graduation. Factory-only, once.
    function start() external {
        if (msg.sender != factory) revert OnlyFactory();
        if (startTime != 0) revert AlreadyStarted();
        startTime = uint64(block.timestamp);
        totalAllocation = token.balanceOf(address(this));
        emit VestingStarted(startTime, totalAllocation);
    }

    /// @notice Return the escrowed tokens to the factory on an aborted raise.
    ///         Only possible before the clock starts.
    function reclaim() external returns (uint256 amount) {
        if (msg.sender != factory) revert OnlyFactory();
        if (startTime != 0) revert AlreadyStarted();
        amount = token.balanceOf(address(this));
        if (amount > 0) token.safeTransfer(factory, amount);
        emit Reclaimed(amount);
    }

    /// @notice Amount unlocked so far along the linear schedule.
    function vestedAmount() public view returns (uint256) {
        if (startTime == 0) return 0;
        uint256 elapsed = block.timestamp - startTime;
        if (elapsed >= duration) return totalAllocation;
        return (totalAllocation * elapsed) / duration;
    }

    /// @notice Unlocked and not yet claimed.
    function claimable() public view returns (uint256) {
        return vestedAmount() - released;
    }

    /// @notice Pull everything currently unlocked to the beneficiary.
    function claim() external returns (uint256 amount) {
        if (msg.sender != beneficiary) revert OnlyBeneficiary();
        if (startTime == 0) revert NotStarted();
        amount = claimable();
        if (amount == 0) revert NothingToClaim();
        released += amount;
        token.safeTransfer(beneficiary, amount);
        emit Claimed(amount);
    }
}
