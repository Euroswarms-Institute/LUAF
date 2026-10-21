// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

/// @title QuiverToken
/// @notice Fixed-supply launchpad token with an on-chain, gas-safe holder
///         dividend tracker. A share of every trade's tax (routed in by the
///         hook) is distributed to holders in proportion to how much they
///         hold, using a MasterChef-style accumulator so distribution is O(1)
///         regardless of holder count. Locked liquidity (held by the V4
///         PoolManager) and other system addresses are excluded so rewards
///         only flow to real holders.
///
///         Rewards are paid in `rewardToken` — any ERC-20 the creator picks at
///         launch (e.g. a tokenized stock), or native currency when set to the
///         zero address. Holders accrue continuously and pull with claim().
contract QuiverToken is ERC20 {
    using SafeERC20 for IERC20;

    uint256 private constant ACC_PRECISION = 1e24;

    /// @notice Wallet credited as the token's creator (immutable attribution).
    address public immutable creator;
    /// @notice The hook allowed to credit dividends; set once by the factory.
    address public hook;
    /// @notice Immutable per-token trade tax in basis points (0..1000 = 0-10%).
    uint16 public immutable taxBps;
    /// @notice Currency dividends are paid in. address(0) == native.
    address public immutable rewardToken;
    /// @notice Balance a holder must keep to earn dividends at all. Below it a
    ///         wallet accrues nothing and its balance leaves the denominator,
    ///         so the forfeited share flows to holders who are above the line.
    ///         0 means every holder earns, which is the plain behaviour.
    uint256 public immutable minHoldForDividends;
    /// @notice 0 = linear (a holder's share is its balance). 1 = tiered, where
    ///         holding a larger multiple of the minimum earns a larger share
    ///         per token, up to 2x. Splitting a balance lowers the multiplier,
    ///         so the ladder cannot be farmed with extra wallets.
    uint8 public immutable dividendMode;

    uint16 private constant W_BPS = 10_000;

    /// @dev Accumulated reward per unit of weight, scaled by ACC_PRECISION.
    uint256 private accRewardPerShare;
    /// @dev Total dividend weight in issue — the denominator every
    ///      distribution divides by. With linear mode and no minimum this is
    ///      exactly the eligible balance supply.
    uint256 public eligibleSupply;
    /// @dev Reward already accounted to a holder: weight * acc / PRECISION.
    mapping(address => uint256) private rewardDebt;
    /// @dev Settled-but-unclaimed rewards per holder.
    mapping(address => uint256) public claimable;
    /// @dev Addresses that do not participate in dividends (pool, system).
    mapping(address => bool) public excluded;

    /// @notice Lifetime rewards distributed to holders, in reward units.
    uint256 public totalRewardsDistributed;

    string private _metadataURI;

    event HookSet(address indexed hook);
    event ExcludedSet(address indexed account, bool excluded);
    event RewardsDistributed(uint256 amount);
    event RewardsClaimed(address indexed holder, uint256 amount);

    error OnlyFactory();
    error OnlyHook();
    error HookAlreadySet();
    error WrongRewardCurrency();

    address private immutable _factory;

    constructor(
        string memory name_,
        string memory symbol_,
        string memory metadataURI_,
        uint256 supply_,
        address creator_,
        address supplyRecipient_,
        uint16 taxBps_,
        address rewardToken_,
        uint256 minHoldForDividends_,
        uint8 dividendMode_
    ) ERC20(name_, symbol_) {
        require(taxBps_ <= 1000, "tax>10%");
        require(dividendMode_ <= 1, "mode");
        _factory = msg.sender;
        creator = creator_;
        taxBps = taxBps_;
        rewardToken = rewardToken_;
        minHoldForDividends = minHoldForDividends_;
        dividendMode = dividendMode_;
        _metadataURI = metadataURI_;

        // Exclude system endpoints (zero, self, and the supply recipient) from
        // dividends up front, so rewards only ever flow to real holders.
        excluded[address(0)] = true;
        excluded[address(this)] = true;
        excluded[supplyRecipient_] = true;

        _mint(supplyRecipient_, supply_);
    }

    /// @notice Off-chain metadata JSON (description, logo, website, socials).
    function metadataURI() external view returns (string memory) {
        return _metadataURI;
    }

    /// @notice Burn tokens held by the caller (used by the hook for buyback&burn).
    function burn(uint256 amount) external {
        _burn(msg.sender, amount);
    }

    // ---------------------------------------------------------------------
    // Factory wiring (one-time)
    // ---------------------------------------------------------------------

    /// @notice Wire the hook and exclude the pool/system addresses. The factory
    ///         calls this exactly once, right after it knows the pool endpoints.
    function initHook(address hook_, address[] calldata excludedAddrs) external {
        if (msg.sender != _factory) revert OnlyFactory();
        if (hook != address(0)) revert HookAlreadySet();
        hook = hook_;
        emit HookSet(hook_);
        _setExcluded(hook_, true);
        for (uint256 i; i < excludedAddrs.length; ++i) {
            _setExcluded(excludedAddrs[i], true);
        }
    }

    // ---------------------------------------------------------------------
    // Dividend distribution
    // ---------------------------------------------------------------------

    /// @notice Credit an ERC-20 reward distribution to all eligible holders.
    ///         The hook must have transferred `amount` of `rewardToken` to this
    ///         contract before calling. No-op-safe when there is no eligible
    ///         supply (caller keeps the funds).
    function distributeRewards(uint256 amount) external {
        if (msg.sender != hook) revert OnlyHook();
        if (rewardToken == address(0)) revert WrongRewardCurrency();
        _distribute(amount);
    }

    /// @notice Credit a native reward distribution to all eligible holders.
    function distributeRewardsNative() external payable {
        if (msg.sender != hook) revert OnlyHook();
        if (rewardToken != address(0)) revert WrongRewardCurrency();
        _distribute(msg.value);
    }

    function _distribute(uint256 amount) private {
        uint256 supply = eligibleSupply;
        if (amount == 0 || supply == 0) return;
        accRewardPerShare += (amount * ACC_PRECISION) / supply;
        totalRewardsDistributed += amount;
        emit RewardsDistributed(amount);
    }

    /// @notice A holder's share of the next distribution. Zero for excluded
    ///         wallets and for anyone under the minimum; otherwise the balance,
    ///         scaled by the tier multiplier when the token runs tiered.
    function dividendWeight(address account) public view returns (uint256) {
        if (account == address(0) || excluded[account]) return 0;
        uint256 bal = balanceOf(account);
        uint256 floor_ = minHoldForDividends;
        if (bal < floor_) return 0;
        if (dividendMode == 0 || floor_ == 0) return bal;
        // A short ladder rather than a curve: three comparisons, legible on a
        // term sheet, and capped so one wallet can never take the whole flow.
        uint256 mult = W_BPS;
        if (bal >= floor_ * 1000) mult = 2 * W_BPS;
        else if (bal >= floor_ * 100) mult = W_BPS + W_BPS / 2;
        else if (bal >= floor_ * 10) mult = W_BPS + W_BPS / 4;
        return (bal * mult) / W_BPS;
    }

    /// @notice Pending, not-yet-settled rewards for a holder.
    function pendingRewards(address holder) public view returns (uint256) {
        if (excluded[holder]) return claimable[holder];
        uint256 accrued = (dividendWeight(holder) * accRewardPerShare) / ACC_PRECISION;
        uint256 debt = rewardDebt[holder];
        uint256 extra = accrued > debt ? accrued - debt : 0;
        return claimable[holder] + extra;
    }

    /// @notice Claim all settled + pending rewards to the caller.
    function claim() external returns (uint256 amount) {
        return _claimTo(msg.sender);
    }

    /// @notice Push a holder's accrued rewards to THEIR wallet. Callable by
    ///         anyone (the protocol keeper calls it after every distribution so
    ///         rewards land in wallets with no user action), but the funds can
    ///         only ever go to the holder — non-custodial by construction.
    function claimFor(address holder) external returns (uint256 amount) {
        return _claimTo(holder);
    }

    /// @notice Batch delivery for the keeper: push rewards to many holders in
    ///         one transaction. A single failing receiver (native rewards only)
    ///         is skipped rather than blocking the whole batch.
    function claimForMany(address[] calldata holders) external {
        for (uint256 i; i < holders.length; ++i) {
            try this.claimFor(holders[i]) {} catch {}
        }
    }

    function _claimTo(address holder) private returns (uint256 amount) {
        _settle(holder);
        amount = claimable[holder];
        if (amount == 0) return 0;
        claimable[holder] = 0;
        emit RewardsClaimed(holder, amount);
        if (rewardToken == address(0)) {
            (bool ok, ) = payable(holder).call{value: amount}("");
            require(ok, "native xfer");
        } else {
            IERC20(rewardToken).safeTransfer(holder, amount);
        }
    }

    // ---------------------------------------------------------------------
    // Accounting hooks
    // ---------------------------------------------------------------------

    /// @dev Move a holder's freshly-accrued rewards into `claimable` and reset
    ///      their debt to the current balance basis.
    function _settle(address account) private {
        if (account == address(0) || excluded[account]) return;
        uint256 accrued = (dividendWeight(account) * accRewardPerShare) / ACC_PRECISION;
        uint256 debt = rewardDebt[account];
        if (accrued > debt) claimable[account] += accrued - debt;
        rewardDebt[account] = accrued;
    }

    function _resetDebt(address account) private {
        rewardDebt[account] = (dividendWeight(account) * accRewardPerShare) / ACC_PRECISION;
    }

    function _setExcluded(address account, bool value) private {
        if (excluded[account] == value) return;
        // Settle then flip participation, adjusting eligibleSupply by balance.
        if (value) {
            _settle(account);
            eligibleSupply -= dividendWeight(account); // weight before the flip
            excluded[account] = true;
        } else {
            excluded[account] = false;
            eligibleSupply += dividendWeight(account);
            _resetDebt(account);
        }
        emit ExcludedSet(account, value);
    }

    /// @dev Core transfer/mint/burn accounting. Settles both sides, moves the
    ///      balance, keeps `eligibleSupply` in sync with participation, and
    ///      rebases each side's reward debt to its new balance.
    function _update(address from, address to, uint256 value) internal override {
        bool fromEligible = from != address(0) && !excluded[from];
        bool toEligible = to != address(0) && !excluded[to];

        if (fromEligible) _settle(from);
        if (toEligible) _settle(to);

        // Weight is a function of the balance, so a transfer can push either
        // side across the minimum or into another tier. Difference the weights
        // around the move rather than assuming the denominator shifts by
        // `value`, which only holds when weight tracks balance one for one.
        uint256 beforeFrom = fromEligible ? dividendWeight(from) : 0;
        uint256 beforeTo = toEligible ? dividendWeight(to) : 0;

        super._update(from, to, value);

        uint256 afterFrom = fromEligible ? dividendWeight(from) : 0;
        uint256 afterTo = toEligible ? dividendWeight(to) : 0;
        eligibleSupply = eligibleSupply + afterFrom + afterTo - beforeFrom - beforeTo;

        if (fromEligible) _resetDebt(from);
        if (toEligible) _resetDebt(to);
    }

    /// @notice Accept native only as reward funding.
    receive() external payable {}
}
