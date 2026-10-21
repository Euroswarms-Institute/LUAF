// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface IQuiverToken is IERC20 {
    function creator() external view returns (address);
    function taxBps() external view returns (uint16);
    function rewardToken() external view returns (address);
    function hook() external view returns (address);
    function eligibleSupply() external view returns (uint256);

    function initHook(address hook_, address[] calldata excludedAddrs) external;
    function distributeRewards(uint256 amount) external;
    function distributeRewardsNative() external payable;
    function burn(uint256 amount) external;
}
