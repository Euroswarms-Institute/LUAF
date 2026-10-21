// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface IVentureListings {
    function listings(address token)
        external
        view
        returns (address creator, address pair, uint16 taxBps, uint64 createdAt, bytes32 poolId);
}

/// @title SyndicateRegistry
/// @notice Which launchpads are inside the syndicate, and who gets paid for
///         the volume their coins do.
///
///         The tenant model's documented moat was that leaving would cost a
///         brand our keepers, our index and our routing. In the contracts it
///         cost them nothing: a tenant gets its own factory, its own hook and
///         its own router, and the fee split is enforced only where the fees
///         happen to be addressed. The cut was a claim, not a mechanism.
///
///         Hilferding describes what an actual one looks like. A cartel held
///         together by a price agreement is weak — "A mere price agreement
///         will only be effective in periods of prosperity" — and the ones
///         that last move their members' selling into "a central sales agency
///         of the cartel", so that "A member enterprise which leaves the
///         cartel has to renew relations with its customers, re-establish its
///         sales outlets", and "such attempts may well fail, or succeed only
///         at considerable cost." The binding is the channel, not the clause:
///         the cartel "binds its members together not only by the terms of the
///         agreement."
///
///         So this registry pairs with AggregatorRouter, the shared channel.
///         Volume routed through it on a member's coin pays that member a share
///         of the router's fee — including volume the member did not bring,
///         because the terminal's own users trade whatever is listed. Leaving
///         forfeits that stream the moment the factory is deregistered.
///
///         Two deliberate properties:
///
///         - **The beneficiary is read live, through the coin's factory.** A
///           per-coin snapshot would keep paying a departed tenant forever on
///           everything they had already launched, which is no exit cost at
///           all.
///         - **Registering a coin is permissionless but verified.** Anyone may
///           register a coin, and the registry checks with the factory that it
///           really launched it. Nobody can point someone else's coin at their
///           own payout.
contract SyndicateRegistry {
    address public immutable admin;

    /// @notice Member launchpad factory to the address paid for its volume.
    ///         Zero means not a member, which is also how a member leaves.
    mapping(address factory => address beneficiary) public beneficiaryOfFactory;

    /// @notice The factory a coin was launched by, once proven.
    mapping(address coin => address factory) public factoryOfCoin;

    event FactorySet(address indexed factory, address indexed beneficiary);
    event CoinRegistered(address indexed coin, address indexed factory);

    error NotAdmin();
    error NotAMember();
    error NotFromThatFactory();
    error AlreadyRegistered();

    constructor(address admin_) {
        if (admin_ == address(0)) revert NotAdmin();
        admin = admin_;
    }

    /// @notice Admit a launchpad to the syndicate, change where it is paid, or
    ///         remove it by passing the zero address.
    function setFactory(address factory, address beneficiary) external {
        if (msg.sender != admin) revert NotAdmin();
        beneficiaryOfFactory[factory] = beneficiary;
        emit FactorySet(factory, beneficiary);
    }

    /// @notice Record that `coin` came from `factory`. Anyone may call this;
    ///         the factory is asked to confirm it, so a caller cannot claim a
    ///         coin it did not launch.
    function registerCoin(address factory, address coin) external {
        if (beneficiaryOfFactory[factory] == address(0)) revert NotAMember();
        if (factoryOfCoin[coin] != address(0)) revert AlreadyRegistered();
        (address creator,,,,) = IVentureListings(factory).listings(coin);
        if (creator == address(0)) revert NotFromThatFactory();
        factoryOfCoin[coin] = factory;
        emit CoinRegistered(coin, factory);
    }

    /// @notice Who to pay for volume on `coin` right now, or zero for nobody.
    ///         Read through the factory on every call, so deregistering a
    ///         launchpad stops the stream for every coin it ever launched.
    function beneficiaryFor(address coin) external view returns (address) {
        address factory = factoryOfCoin[coin];
        if (factory == address(0)) return address(0);
        return beneficiaryOfFactory[factory];
    }
}
