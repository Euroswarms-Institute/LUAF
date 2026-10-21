// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface IVentureListings {
    function listings(address token)
        external
        view
        returns (address creator, address pair, uint16 taxBps, uint64 createdAt, bytes32 poolId);
}

/// @title VentureUpdates
/// @notice On-chain investor relations: founders post progress updates for
///         their venture, gated to the venture's creator. Event-only — no
///         storage to bloat, spam bounded by gas — so the feed is exactly the
///         event log and any indexer or the app can replay it.
contract VentureUpdates {
    IVentureListings public immutable factory;

    event UpdatePosted(address indexed token, address indexed author, string update);

    error NotCreator();

    constructor(IVentureListings factory_) {
        factory = factory_;
    }

    /// @param update Freeform text or a JSON blob (title/body/link), kept
    ///        small — it lives in calldata and the event log only.
    function postUpdate(address token, string calldata update) external {
        (address creator,,,,) = factory.listings(token);
        if (msg.sender != creator) revert NotCreator();
        emit UpdatePosted(token, msg.sender, update);
    }
}
