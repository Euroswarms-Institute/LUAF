# Deploying

This is the order the stack goes up in, the variables each step reads, and what
to check before moving on. It was rehearsed end to end against a fork of
Robinhood Chain testnet (chain 46630) at block ~140.2M; every command below ran
there and the output quoted is what it printed.

## What cannot be undone

Four decisions are permanent. None of them has a setter, and the factory
renounces its own ownership as the last step of its deploy.

| Decision | Set by | Consequence of getting it wrong |
| --- | --- | --- |
| `platformTreasury` in the hook | `deploy-tenant.ts`, as the tenant `FeeSplitter` | every trade fee on every graduated pool from this stack, forever, goes to that address |
| `tenant` / `protocol` / `protocolBps` in a `FeeSplitter` | `deploy-tenant.ts` and `deploy-launcher.ts` | the split cannot be changed; the money arrives where the constructor said |
| `admin` in `SyndicateRegistry` | `deploy-aggregator.ts` | only that address can ever admit or remove a launchpad |
| `operator` in `Launcher` | `deploy-launcher.ts` | only that key can ever launch through it |

Graduated liquidity is add-only and owned by the factory, so a coin launched on
a misrouted stack cannot be moved to a correct one. The cost of a typo here is
the whole future income of that stack, not a redeploy.

Everything else — `graduationRaiseWei`, `creationFeeWei`, pause — is `setParams`
on the factory and belongs to `ADMIN`.

## Addresses you need before you start

Decide these first and write them down; three of them are in the table above.

- `ADMIN` — a long-lived key or multisig. It holds `setParams` on the factory
  and, if you give it the same value there, `SyndicateRegistry.admin`. Do not
  use the deployer key: it is hot, it signs twelve transactions during the
  deploy, and it can never be rotated out of the registry.
- `TENANT_TREASURY`, `PROTOCOL_TREASURY` — where the platform fee splits to.
  Plain EOAs are simplest; a contract here must accept a bare ETH transfer or
  every ETH-denominated fee to it reverts.
- `OPERATOR` — the pipeline's hot wallet, the only address `Launcher.launch`
  accepts. Fund it: it pays for each launch and its first buy.
- `TREASURY` for the aggregator router — can be the same as
  `PROTOCOL_TREASURY`.
- `PRIVATE_KEY` — the deployer. It needs far less than it looks: the three
  deploys were measured at **16,825,807 gas** in total, which at the live gas
  prices is **0.00017 ETH on testnet** and **0.00036 ETH on mainnet**. Preflight
  prices this from the chain rather than asserting a round number.

## The chains

```
mainnet  RPC_URL=https://rpc.mainnet.chain.robinhood.com  CHAIN_ID=4663
testnet  RPC_URL=https://rpc.testnet.chain.robinhood.com  CHAIN_ID=46630
```

Both are in `INFRA` in `contracts/scripts/lib/venture-stack.ts`, and both
entries' PoolManager and WETH were checked against the live chains by
`preflight` (testnet WETH `0x33e4…0B94`, mainnet WETH `0x0Bd7…AD73`; the V4
PoolManager is at the same address on both). The testnet has no V3 router, so
launches there are WETH-paired only.

Everything runs with `--network target`, which is built from `RPC_URL`,
`CHAIN_ID` and `PRIVATE_KEY`. There is no network named after a chain.

## Rehearse it first

A rehearsal is the same commands against a forked node, so the only thing that
differs from the real run is who pays. Do this before every mainnet deploy, not
only the first.

```bash
cd contracts
FORK_URL=https://rpc.mainnet.chain.robinhood.com FORK_CHAIN_ID=4663 \
  npx hardhat node --hostname 127.0.0.1 --port 8545
```

Then in another shell, with `RPC_URL=http://127.0.0.1:8545`, the deployer set to
one of the node's printed keys, and `DEPLOYMENTS_DIR` pointed somewhere
disposable, run steps 0–4 below exactly as written.

The scripts detect a development node (`hardhat_metadata` / `anvil_nodeInfo`
answer only there) and write `…​.fork.json` with `"fork": true`, so a
rehearsal's addresses can never be picked up later as a deployment.

## What things cost

Measured on a fork of testnet 46630, not estimated. Gas prices as read from the
chains: 0.01 gwei on testnet 46630, 0.0213 gwei on mainnet 4663.

| | gas | testnet | mainnet |
| --- | --- | --- | --- |
| tenant stack (10 txs) | 13,551,813 | 0.000136 ETH | 0.000289 ETH |
| launcher (2 txs) | 1,493,277 | 0.000015 ETH | 0.000032 ETH |
| aggregator (2 txs) | 1,780,717 | 0.000018 ETH | 0.000038 ETH |
| **all three** | **16,825,807** | **0.000168 ETH** | **0.000359 ETH** |
| one launch + first buy | ~1,950,000 | 0.000020 ETH | 0.000042 ETH |

Reproduce the launch figure with `scripts/measure-launch.ts` against a deployed
stack on a fork. The per-launch number is also the floor the selection loop's
reaper measures a format against: fees per copy below the cost of deploying
another copy means the format is not worth keeping.

### One coin, launch to earning

`scripts/rehearse-lifecycle.ts` runs the whole money path against a deployed
stack: launch through the `Launcher`, buy to graduation, trade the graduated
pool, pull the fees into the splitter, split them. On a stack deployed with
`MIN_TARGET_ETH=0.0001 GRADUATION_RAISE_ETH=0.0001` and the band's top ETH price
it came to **0.00017 ETH** in total — 5,491,949 gas (0.000055 ETH) plus
0.000115 ETH of principal, of which the 0.0001 ETH raise is gone for good:
graduated liquidity is owned by the factory and can only be added to.

What came out the other end, on the smallest raise the curve will carry:

```
platform splitter   0.00000045 ETH in the factory ledger + 279,003 of the coin
launcher (creator)  0.00000005 ETH in the factory ledger
split               tenant 0.00000041 / protocol 0.000000045  (1000 bps, exact)
keeper              sold 25% of the coin for 0.0000359 ETH, 3 txs, 0 failures
```

The fees arriving in the coin rather than in ETH is the design, not a fault,
and the keeper converting them is the step that makes them money.

So a day's faucet of 0.01 ETH funds the whole stack plus about fifty complete
coin lifecycles. Funding was never the constraint; the graduation target is,
because that part is spent.

## Two numbers that are coupled

Neither is visible from the other side, and together they are how a stack gets
deployed that cannot launch anything.

**The graduation target has a floor set by the ETH price.** The factory starts
every coin at an FDV of $750 across a supply of 1e9, and the curve's slope is
`2*(target - p0*supply)/supply²`, so a target below the start FDV underflows and
every Open-mode launch reverts with `InvalidParams`. In ETH that floor is
`$750 / price`:

| ETH price | smallest graduation target that works |
| --- | --- |
| $3,000 | 0.25 ETH |
| $10,000 | 0.075 ETH |

So `GRADUATION_RAISE_ETH=0.004`, which looks like a sensible dust-sized target,
is a stack on which nothing can launch at any honest ETH price. It works on
testnet only because the deployed ETH/USD band tops out at $10,000,000 and the
launcher passes a fictitious price. Set `MIN_TARGET_ETH` and
`GRADUATION_RAISE_ETH` above the floor for the price you intend to launch at,
and set `ETH_USD` when running preflight so it checks them.

**The first buy is capped at `target/100`.** `Launcher.launch` buys inside the
factory's 60-second launch window, where the per-wallet cap is
`targetRaiseWei / 100`. A first buy over that reverts the whole launch, mined
salt and all. The operator's opening position is bounded by the graduation
target, not by what it is willing to spend.

Both were found by a launch reverting on a fork. Both are now preflight checks.

## The router needs gas headroom, not an estimate

A `VentureRouter` trade sent at exactly its own estimated gas limit **runs out
of gas**. Measured against a freshly graduated pool on a fork of 46630:

```
estimateGas             849,368
limit   849,368         FAILED: ran out of gas
limit   900,000         OK, used 785,359
limit 2,000,000         OK, used 785,359
```

The estimate is *higher* than the gas the transaction actually consumes, and it
still fails at that limit. The trade runs inside `PoolManager.unlock`, so the
work happens in a nested call, which EIP-150 gives at most 63/64 of the gas
remaining at the call; with a tight limit the inner frame is starved even though
the total would have been enough. How much gas is consumed therefore depends on
the limit it was sent with, which is the one thing `estimateGas` cannot measure,
because it binary-searches on the total.

It bites hardest where it costs most. The first trade on a newly graduated pool
needs the most gas, and the keeper's first sale of a coin's fee income is often
that trade — so without headroom the keeper fails exactly when fees first exist.
`keeper/venture-treasury.mjs` now sends router trades and `redeem6909` at 1.25x
the estimate (`GAS_HEADROOM_BPS`). Anything else that trades through the router,
including a front end, needs the same.

## Rehearsing against a fork: two settings

- `CONFIRMATIONS=1` for the keeper. The default of 2 is right for a real chain
  and hangs forever on an automine node, which mines a block per transaction and
  so never produces the second confirmation.
- `SWAP_GAS_LIMIT` in the lifecycle script, for the headroom above.

`CONFIRMATIONS=0` is rejected: `wait(0)` returns without waiting for inclusion,
so every later transaction in the run reuses a nonce the previous one took. It
failed three of eleven transactions the first time it was tried.

## Step 0 — preflight

```bash
cd contracts
RPC_URL=… CHAIN_ID=… PRIVATE_KEY=0x… \
ADMIN=0x… TENANT_TREASURY=0x… PROTOCOL_TREASURY=0x… OPERATOR=0x… \
PROTOCOL_BPS=1000 ETH_USD=3000 \
MIN_TARGET_ETH=… GRADUATION_RAISE_ETH=… \
npm run preflight
```

Read-only, costs nothing, and exits non-zero on anything that would make the
deploy wrong: a `CHAIN_ID` that disagrees with the RPC, no signer, a deployer
whose balance is under twice the measured gas cost at the live price,
infrastructure with no code at the address, a WETH that is not an ERC-20, a zero
or unparseable immutable, a bps outside its range, a graduation target under the
curve's start FDV. It warns
where a choice is legal but probably not what you want — `ADMIN` equal to the
deployer, a contract treasury, an unfunded operator.

Do not proceed with failures. Warnings are yours to accept.

## Step 1 — the tenant stack

```bash
TENANT_SLUG=acme \
TENANT_TREASURY=0x… PROTOCOL_TREASURY=0x… PROTOCOL_BPS=1000 \
ADMIN=0x… PLATFORM_FEE_BPS=55 REF_SHARE_BPS=2000 \
MIN_TARGET_ETH=0.3 GRADUATION_RAISE_ETH=0.5 CREATION_FEE_ETH=0 \
npm run deploy:tenant
```

Those two targets are above the 0.25 ETH curve floor at $3,000/ETH. Do not copy
the 0.002/0.004 pair used in the fork rehearsals without reading the coupling
section above: at an honest price they deploy a stack that rejects every launch.

Ten transactions, nine if `ADMIN` is not the deployer: the `FeeSplitter`, a
CREATE2 deployer, the vesting deployer, the hook at a mined address (the V4 flag
bits live in the address, so the salt is ground for), the token deployer, the
factory, the router, `VentureUpdates`, `setParams`, and the ownership renounce.
It writes `contracts/deployments/tenant-<slug>-<chainId>.json`.

`MIN_TARGET_ETH` is immutable: it is the smallest raise the platform will ever
finish on this stack. `GRADUATION_RAISE_ETH` and `CREATION_FEE_ETH` go in by
`setParams`, which the script sends only when the deployer is `ADMIN`.
Otherwise it prints the exact call for `ADMIN` to send, and **until that is sent
the stack runs on the contract defaults — 4 ETH graduation — not on what you
asked for**. `setParams` is gated on `protocolAdmin`, not on ownership, so
renouncing does not close the door; the script's own printed
`graduationRaiseWei:` line tells you which of the two is in effect.

The last line of a good run is `factory ownership renounced`. If the run dies
before it, the factory is still owned and the artifact may not describe it:
re-read it with step 4 before using it for anything.

## Step 2 — the launcher

```bash
TENANT_DEPLOYMENT=deployments/tenant-acme-4663.json \
OPERATOR=0x… PROTOCOL_BPS=1000 \
npm run deploy:launcher
```

A creator `FeeSplitter` and the `Launcher`, written to
`launcher-<slug>-<chainId>.json`. The artifact also records the exact
`QuiverToken` creation bytecode, because the pipeline mines a CREATE2 vanity
salt against it for every launch: if it does not match the deployed token
deployer's, every launch reverts on the vanity check. Step 4 checks it.

## Step 3 — the aggregator

```bash
TREASURY=0x… ADMIN=0x… FEE_BPS=100 REBATE_BPS=3000 \
npm run deploy:aggregator
```

`SyndicateRegistry` and `AggregatorRouter`, written to
`aggregator-<chainId>.json`. `FEE_BPS` is capped at 100 (1%) in the contract.
`REBATE_BPS` is the share of that fee a member launchpad is paid.

The registry starts empty and pays nobody until it is populated:

```
registry.setFactory(<factory>, <beneficiary>)   // from ADMIN
```

Our own tenant factories go in it too — that is what makes the terminal's
volume pay the stack that launched the coin.

## Step 4 — verify

```bash
TENANT_DEPLOYMENT=deployments/tenant-acme-4663.json \
LAUNCHER_DEPLOYMENT=deployments/launcher-acme-4663.json \
AGGREGATOR_DEPLOYMENT=deployments/aggregator-4663.json \
npm run verify:deployment
```

Reads all of it back off the chain and checks every wire against the artifact:
the hook's treasury, launcher binding and fee bps; the factory's hook, pool
manager, WETH, deployers, admin, `minTargetWei`, curve fees, vanity mark and
that its ownership really is renounced; the router's factory and hook; both
splitters' recipients and split; the `Launcher`'s operator and splitter; the
recorded token bytecode; the registry's admin and the aggregator router's
registry, treasury and fee.

A successful deploy transaction says nothing about where the money is addressed.
This does. It is pure reads, so run it against mainnet freely, and again any
time an artifact is handed to something new.

It was run against the venture stack already live on testnet 46630 (factory
`0x60e0…963b`, three coins) and every wire matched, so the checks are known to
hold against a real deployed stack and not only against a rehearsal's.

## Then the off-chain half

Nothing above earns anything on its own. In order:

1. **Keeper.** Commit the stack's `tenant-<slug>-<chainId>.json` under
   `contracts/deployments/` (addresses only, no secrets), set the repository
   variable `KEEPER_DEPLOYMENT` to its path relative to `keeper/`, and add the
   `VENTURE_KEEPER_PK` secret for a funded keeper wallet. Without both,
   `.github/workflows/keeper.yml` skips every run and says so. It then converts
   fee income to ETH on the regime's schedule, services the splitter, and
   registers graduations with the registry — a registry nobody populates pays
   nobody.
2. **Aggregator.** Add the new factory to `aggregator/sources.json` as a
   `venture` source with the artifact's `startBlock`, then run the indexer.
   `sources.testnet.json` is a working example.
3. **Service.** Point `LUCRE_LAUNCHER_DEPLOYMENT` at the launcher artifact and
   give the tenant a grant in `LUCRE_TENANTS_FILE`. A grant with no signing
   token is a dry run, which is the right first state.

## Deployed: testnet 46630

Live, verified, and earning. Everything below is on-chain, not a rehearsal.

| | |
| --- | --- |
| tenant | `lucre`, protocolBps 1000 |
| factory | `0x2707Ed204ABB8A8d743cBb368dD06b9050bC2D86` |
| hook | `0x165a16DeB56cCefc0C5c34cBF027093a857EA044` |
| router | `0xaD5459ad2f5185AF502AD0681b032daD06eABaB7` |
| platform splitter | `0x5da7bD3E6e45C410beFD0Ea56E7daA9303D4E8B8` |
| launcher | `0x514A18b9E0e45525C3e6A49eF737699dAcbED56e` |
| creator splitter | `0xc05816A925E01AFF26cB5A6b7e20e82F9920f3F0` |
| syndicate registry | `0x4F8f43e2C4E6aE0fdE58BfaE7Dff18390847983d` |
| aggregator router | `0x5F6d6050096ee1b9e9676E9cF9946bb82f40c100` |
| first coin | `0x2aDDc092Eba4D465118d7D42526923B5b60d6F1A`, graduated |

`minTargetWei` is 0.0001 ETH. It is immutable and `setParams` can only raise
`graduationRaiseWei` above it, so the lowest workable floor is the most flexible
choice; the launches pass the band's top ETH price to clear the curve floor.

The factory is admitted to the registry with the protocol treasury as
beneficiary, so routed volume on its coins pays. The keeper ran against it: four
transactions, one split, one coin registered, one coin sold for 0.0000358 ETH,
no failures. The aggregator indexes it from block 140770636 as the
`lucre-testnet` source, alongside three third-party coins.

The first revenue the business has had, read off the chain:

```
tenant   0x1E5b…a622   0.000000407440 ETH
protocol 0xC184…7d16   0.000000045271 ETH   exactly 1000 bps
```

The whole thing — three deploys, a coin from launch to graduated and traded, the
registry admission and a keeper run — cost 0.000314 ETH of the 0.01 funded.

The deployer key for this stack is disposable and testnet-only. It is also the
factory's `protocolAdmin` and the registry's immutable `admin`, which is
acceptable for a stack whose purpose is to produce the first real fee numbers
and unacceptable for mainnet.

### To make the keeper run on its own

`.github/workflows/keeper.yml` skips every run until both exist:

- secret `VENTURE_KEEPER_PK` — a funded keeper wallet
- variable `KEEPER_DEPLOYMENT` = `../contracts/deployments/tenant-lucre-46630.json`

## Known decision, not yet made

The aggregator's premise was that Robinhood Chain had no trading terminal. Axiom
and GMGN both arrived in July 2026. Which chain goes first is a business call
and is not settled in this repository.
