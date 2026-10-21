// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";

/// @notice Test double for the slice of the V4 PoolManager a fee recipient
///         touches when it redeems ERC-6909 claims: unlock -> callback ->
///         burn + take. It holds real ERC-20 balances so take() pays out.
contract MockClaimsManager {
    mapping(address owner => mapping(uint256 id => uint256)) public balanceOf;
    bool internal unlocked;

    /// @dev Stands in for the hook's `mint` fallback in `_payOut`.
    function mint(address to, uint256 id, uint256 amount) external {
        balanceOf[to][id] += amount;
    }

    function unlock(bytes calldata data) external returns (bytes memory result) {
        unlocked = true;
        result = IUnlockCallback(msg.sender).unlockCallback(data);
        unlocked = false;
    }

    function burn(address from, uint256 id, uint256 amount) external {
        require(unlocked && msg.sender == from, "locked");
        balanceOf[from][id] -= amount;
    }

    function take(address currency, address to, uint256 amount) external {
        require(unlocked, "locked");
        IERC20(currency).transfer(to, amount);
    }
}
