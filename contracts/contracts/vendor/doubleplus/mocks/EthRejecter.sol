// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Test double for a creator that cannot receive ETH: it forwards
///         arbitrary calls (so it can launch a venture as msg.sender) and has
///         no receive or fallback, so any ETH pushed to it reverts.
contract EthRejecter {
    function exec(address target, bytes calldata data) external payable returns (bytes memory) {
        (bool ok, bytes memory ret) = target.call{value: msg.value}(data);
        if (!ok) {
            assembly { revert(add(ret, 32), mload(ret)) }
        }
        return ret;
    }
}
