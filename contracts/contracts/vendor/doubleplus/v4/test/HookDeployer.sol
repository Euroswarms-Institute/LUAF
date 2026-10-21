// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @dev Minimal CREATE2 deployer used only in tests/scripts to place the hook
///      at an address whose low bits encode its V4 permission flags.
contract HookDeployer {
    event Deployed(address addr);

    function deploy(bytes32 salt, bytes memory code) external returns (address addr) {
        assembly {
            addr := create2(0, add(code, 0x20), mload(code), salt)
        }
        require(addr != address(0), "create2 failed");
        emit Deployed(addr);
    }
}
