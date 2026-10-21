// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {FounderVesting} from "./FounderVesting.sol";

/// @notice Bytecode isolator: keeps FounderVesting's creation code out of the
///         factory so the factory stays under the EIP-170 size limit. Open to
///         any caller — the escrow binds to whoever called deploy(), and the
///         launchpad only trusts the escrows it created itself.
contract VestingDeployer {
    function deploy(address token, address beneficiary, uint64 duration) external returns (FounderVesting v) {
        v = new FounderVesting(token, beneficiary, duration, msg.sender);
    }
}
