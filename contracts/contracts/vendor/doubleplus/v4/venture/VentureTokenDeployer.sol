// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {QuiverToken} from "../QuiverToken.sol";

/// @title VentureTokenDeployer
/// @notice Deploys QuiverToken instances for the venture factory. Split out so
///         the factory stays under the contract size limit; only the factory
///         can call, supply always mints to the factory, and the factory keeps
///         the one-time initHook authority through the forwarder below
///         (QuiverToken grants that authority to its deployer).
contract VentureTokenDeployer {
    address public immutable factory;

    error OnlyFactory();
    error FloorTooHigh();
    error BadDividendPolicy();

    uint256 private constant TOTAL_SUPPLY_WHOLE = 1_000_000_000;

    constructor(address factory_) {
        factory = factory_;
    }

    function deployToken(
        bytes32 salt,
        string calldata name_,
        string calldata symbol_,
        string calldata metadataURI_,
        uint256 supply_,
        address creator_,
        uint16 taxBps_,
        address rewardToken_,
        uint256 minHoldForDividends_,
        uint8 dividendMode_,
        uint16 dividendBps_
    ) external returns (address token) {
        if (msg.sender != factory) revert OnlyFactory();
        // The floor arrives in whole tokens and is scaled here; cap it at 1%
        // of supply so a launch cannot price every ordinary holder out of its
        // own dividends.
        if (minHoldForDividends_ > TOTAL_SUPPLY_WHOLE / 100) revert FloorTooHigh();
        // A floor or a ladder is meaningless when no fee reaches holders, and a
        // ladder needs a floor to be a multiple of.
        if (dividendBps_ == 0 && (minHoldForDividends_ != 0 || dividendMode_ != 0)) revert BadDividendPolicy();
        if (dividendMode_ == 1 && minHoldForDividends_ == 0) revert BadDividendPolicy();
        token = address(
            new QuiverToken{salt: salt}(
                name_, symbol_, metadataURI_, supply_, creator_, factory, taxBps_, rewardToken_,
                minHoldForDividends_ * 1e18, dividendMode_
            )
        );
    }

    function initHook(address token, address hook_, address[] calldata excludedAddrs) external {
        if (msg.sender != factory) revert OnlyFactory();
        QuiverToken(payable(token)).initHook(hook_, excludedAddrs);
    }
}
