// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {IPoolManager} from "@uniswap/v4-core/src/interfaces/IPoolManager.sol";
import {IUnlockCallback} from "@uniswap/v4-core/src/interfaces/callback/IUnlockCallback.sol";
import {Currency} from "@uniswap/v4-core/src/types/Currency.sol";

interface IFeeLedger {
    function feesAccrued(address recipient) external view returns (uint256);
    function withdrawFees() external returns (uint256);
}

/// @title FeeSplitter
/// @notice The platform treasury of a tenant venture stack. The tenant's hook
///         and factory are deployed with this contract as `platformTreasury`,
///         so every protocol fee the stack earns lands here and leaves split
///         `protocolBps` to the protocol, the rest to the tenant. Nothing in
///         VentureFactory or VentureFeeHook changes: the cut is enforced by
///         where the fees are addressed, not by an agreement.
///
///         Fees arrive four ways:
///           - ETH from the factory's fee ledger (`pullFactory`),
///           - pool currencies (WETH, the coin) from the hook's `take`,
///           - ERC-6909 claims when the hook's `take` fell back to `mint`
///             (`redeem6909` turns them into tokens),
///           - plain transfers.
///         `split` divides whatever unassigned balance of one asset is held.
///
///         Each share is credited first, then pushed with a gas cap. A
///         recipient that rejects or burns gas keeps its credit for `claim`
///         and cannot block the other side's payout.
contract FeeSplitter is IUnlockCallback, ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint16 internal constant BPS = 10_000;
    uint256 internal constant PUSH_GAS = 100_000;

    address public immutable tenant;
    address public immutable protocol;
    uint16 public immutable protocolBps;
    IPoolManager public immutable poolManager;

    /// @dev Credited but not yet paid, per recipient and asset (0 = ETH).
    mapping(address recipient => mapping(address asset => uint256)) public owed;
    /// @dev Sum of `owed` per asset: held, but no longer up for splitting.
    mapping(address asset => uint256) public totalOwed;

    event Split(address indexed asset, uint256 toTenant, uint256 toProtocol);
    event Paid(address indexed recipient, address indexed asset, uint256 amount);
    event Deferred(address indexed recipient, address indexed asset, uint256 amount);
    event Redeemed(address indexed currency, uint256 amount);

    error BadConfig();
    error NotPoolManager();
    error NothingOwed();
    error TransferFailed();

    constructor(address tenant_, address protocol_, uint16 protocolBps_, IPoolManager poolManager_) {
        if (tenant_ == address(0) || protocol_ == address(0) || protocolBps_ > BPS) revert BadConfig();
        tenant = tenant_;
        protocol = protocol_;
        protocolBps = protocolBps_;
        poolManager = poolManager_;
    }

    receive() external payable {}

    /// @notice Withdraw this treasury's ETH from a venture factory's fee ledger.
    function pullFactory(address factory) external nonReentrant returns (uint256 amount) {
        if (IFeeLedger(factory).feesAccrued(address(this)) == 0) return 0;
        amount = IFeeLedger(factory).withdrawFees();
    }

    /// @notice Convert ERC-6909 claims the hook minted here into the token.
    function redeem6909(address currency, uint256 amount) external nonReentrant {
        if (address(poolManager) == address(0)) revert BadConfig();
        poolManager.unlock(abi.encode(currency, amount));
        emit Redeemed(currency, amount);
    }

    function unlockCallback(bytes calldata data) external override returns (bytes memory) {
        if (msg.sender != address(poolManager)) revert NotPoolManager();
        (address currency, uint256 amount) = abi.decode(data, (address, uint256));
        Currency c = Currency.wrap(currency);
        poolManager.burn(address(this), c.toId(), amount);
        poolManager.take(c, address(this), amount);
        return "";
    }

    /// @notice Unassigned balance of `asset` (0 = ETH) up for splitting.
    function splittable(address asset) public view returns (uint256) {
        uint256 held = asset == address(0) ? address(this).balance : IERC20(asset).balanceOf(address(this));
        return held - totalOwed[asset];
    }

    /// @notice Split the unassigned balance of one asset and pay both sides.
    function split(address asset) external nonReentrant returns (uint256 toTenant, uint256 toProtocol) {
        uint256 amount = splittable(asset);
        if (amount == 0) return (0, 0);
        toProtocol = (amount * protocolBps) / BPS;
        toTenant = amount - toProtocol;
        _credit(protocol, asset, toProtocol);
        _credit(tenant, asset, toTenant);
        emit Split(asset, toTenant, toProtocol);
        _push(protocol, asset);
        _push(tenant, asset);
    }

    /// @notice Pull a credit a push could not deliver.
    function claim(address asset) external nonReentrant returns (uint256 amount) {
        amount = owed[msg.sender][asset];
        if (amount == 0) revert NothingOwed();
        _debit(msg.sender, asset, amount);
        if (asset == address(0)) {
            (bool ok,) = payable(msg.sender).call{value: amount}("");
            if (!ok) revert TransferFailed();
        } else {
            IERC20(asset).safeTransfer(msg.sender, amount);
        }
        emit Paid(msg.sender, asset, amount);
    }

    function _credit(address to, address asset, uint256 amount) private {
        if (amount == 0) return;
        owed[to][asset] += amount;
        totalOwed[asset] += amount;
    }

    function _debit(address to, address asset, uint256 amount) private {
        owed[to][asset] -= amount;
        totalOwed[asset] -= amount;
    }

    /// @dev Try to deliver `to`'s whole credit; on failure it stays claimable.
    function _push(address to, address asset) private {
        uint256 amount = owed[to][asset];
        if (amount == 0) return;
        _debit(to, asset, amount);
        bool ok;
        if (asset == address(0)) {
            (ok,) = payable(to).call{value: amount, gas: PUSH_GAS}("");
        } else {
            bytes memory ret;
            (ok, ret) = asset.call{gas: PUSH_GAS}(abi.encodeCall(IERC20.transfer, (to, amount)));
            ok = ok && (ret.length == 0 || abi.decode(ret, (bool)));
        }
        if (ok) {
            emit Paid(to, asset, amount);
        } else {
            _credit(to, asset, amount);
            emit Deferred(to, asset, amount);
        }
    }
}
