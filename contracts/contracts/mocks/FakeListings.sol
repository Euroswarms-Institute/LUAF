// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @notice Test double for the slice of a venture factory the syndicate
///         registry asks about: whether it launched a given coin.
contract FakeListings {
    mapping(address coin => address creator) public creatorOf;

    function setCreator(address coin, address creator) external {
        creatorOf[coin] = creator;
    }

    function listings(address token)
        external
        view
        returns (address creator, address pair, uint16 taxBps, uint64 createdAt, bytes32 poolId)
    {
        return (creatorOf[token], address(0), 0, 0, bytes32(0));
    }
}
