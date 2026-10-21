# LUAF

**One command: a brief becomes a working autonomous unit, a coin on a launchpad we hold, and a paid
endpoint.** Selection then decides what gets launched next, from what the last launches actually earned.

Formerly a Python pipeline that published to a third-party agent marketplace and tokenized on
Solana. Both rented someone else's ground: the listing, the price and the billing relationship
belonged to the platform, its outages became ours, and the Solana path required handing a raw wallet
key to a remote API. Generated agents were also forced to import that platform's SDK, so every
launched unit inherited its dependency.

None of that is left. This version launches into [DoublePlus](https://github.com/IlumCI/DoublePlus)
venture stacks, where the coin's creator is a contract we deploy and the fees are split by code, and
the agents it writes depend on nothing but what their own task needs.

---

## What it does

```
brief ──► research ──► design ──► validate ──► launch ──► endpoint ──► income ──┐
  ▲                                                                              │
  └──────────────────────────── selection ◄─────────────────────────────────────┘
```

1. **Brief** — you supply one, or the profile's topic prompt generates it.
2. **Research** — real search results, biased toward APIs that need no key, so the agent can
   actually run once it is launched.
3. **Design** — an LLM writes the agent against a profile's prompts. Short, stubbed or placeholder
   code is rejected and the complaints are fed back for the next attempt.
4. **Validate** — the agent runs in a subprocess with a **stripped environment**. A design nobody
   has reviewed cannot reach the operator's keys or wallet. Missing dependencies are installed into
   a dedicated virtualenv and the run is retried.
5. **Launch** — the `Launcher` contract opens an Open-mode curve on a tenant's `VentureFactory`,
   with itself as creator, and makes the operator's opening buy in the same transaction.
6. **Endpoint** — a deployable bundle that charges per call over x402: no payment, no run.
7. **Income** — fee events are read back per coin and rolled up per spec.
8. **Selection** — specs that earn breed; specs whose format has saturated are reaped.

## Why the money works this way

| Layer | Who earns | Recurring? |
| --- | --- | --- |
| Curve fees, creator share | the `Launcher`, split 90 tenant / 10 protocol | no — the curve is bounded |
| Founder-tax dev bucket | the same splitter | only while the coin trades |
| Platform fee on every swap | the tenant stack's own splitter | **yes** — graduated liquidity can never migrate |
| Endpoint calls | the creator splitter, in USDC | yes, and it is the only income from outside the trader pool |

Each graduation adds a toll booth that cannot be moved, so the pipeline optimises for graduations
rather than launches.

## Quick start

```bash
npm install
npm run compile                 # vendored venture contracts + Launcher
npm test                        # 7 contract tests, 57 core tests

npx luaf init                   # writes a .env to fill in
npx luaf doctor                 # model, profiles, research, chain
npx luaf run --design-only      # design and validate, touch no chain
npx luaf run                    # dry run: simulates the real launch, signs nothing
npx luaf run --no-dry-run       # signs
```

Nothing is signed without `--no-dry-run`. A dry run still simulates the launch against the real
factory, so it fails for the same reasons a live one would — a paused factory, an ETH/USD price
outside the deployed band, a first buy over the launch-window cap.

## Layout

| Path | What it is |
| --- | --- |
| `contracts/contracts/Launcher.sol` | creator of every coin the pipeline launches; forces creator income to the splitter and tags each launch with its `specId` |
| `contracts/contracts/vendor/doubleplus/` | the DoublePlus venture stack, vendored so the Launcher is tested against the contracts it will actually meet. Re-sync with `contracts/scripts/sync-doubleplus.sh` |
| `contracts/scripts/deploy-launcher.ts` | reads a DoublePlus `tenant-<slug>-<chain>.json`, deploys a creator `FeeSplitter` and the `Launcher`, writes `launcher-<slug>-<chain>.json` |
| `core/src/spec.ts` | the launch spec: the unit of work, and the genome selection breeds |
| `core/src/pipeline.ts` | one run, end to end |
| `core/src/selection.ts` | frequency-dependent fitness, the reaper, crossover, order-2 mutation |
| `core/src/income.ts` | fee events per coin and per spec |
| `core/src/publish/evm.ts` | salt mining and the launch |
| `core/src/publish/endpoint.ts` | the x402 bundle |
| `cli/src/main.ts` | the command line |
| `core/data/profiles/` | the designer's prompt sets, one directory per profile |

## Deploying for a tenant

A tenant first needs a venture stack, deployed from DoublePlus
(`contracts/scripts/deploy-tenant.ts`). Then:

```bash
cd contracts
RPC_URL=… CHAIN_ID=4663 PRIVATE_KEY=0x… \
TENANT_DEPLOYMENT=../../DoublePlus/contracts/deployments/tenant-acme-4663.json \
OPERATOR=0x… PROTOCOL_BPS=1000 \
npx hardhat run scripts/deploy-launcher.ts --network target
```

This writes `contracts/deployments/launcher-acme-4663.json`, which carries both stacks' addresses
and the exact `QuiverToken` creation bytecode the pipeline mines salts against — so a release never
depends on build artifacts and can never mine against a bytecode the chain disagrees with.

## Selection

Fitness is a **rate**: what the next copy of a format is worth per year, in wei. Three terms:

```
fitness = (fees / live copies) x (turnovers per year) x (depreciation)
```

1. **Income per live copy** prices saturation in, counting rivals as well as our own. A format with
   ten times the gross income and a hundred times the copies scores a tenth as much, because the
   question is what the *next* copy is worth, not what the last hundred were.
2. **Turnovers per year** is Marx's `n`. Capital II ch. 16 gives the arithmetic outright — the annual
   rate of surplus value "is always equal to s'n", and two capitals alike in everything but turnover
   period differ by "900% in the annual rate". A format that graduates in three days and one that
   takes ninety are not equally good businesses; scoring them alike was this loop's largest blind
   spot. A coin still on the curve has an *open* turnover and is counted against the format at its
   elapsed time: capital in circulation, per Capital II ch. 5, "produces neither commodities nor
   surplus-value". Capped at one turnover a day, so a single lucky fast graduation cannot dominate.
3. **Depreciation** discounts earnings by age, half-life 30 days. Capital I ch. 15 on machinery: it
   "loses exchange-value, either by machines of the same sort being produced cheaper than it, or by
   better machines entering into competition with it." What a format took months ago says little
   about what it would take now — and with models replaced every few weeks, that applies to agents
   harder than to looms.

The reap test is deliberately **not** this rate. Whether a format covered what a launch cost is a
cash question, so it compares income per copy against `costPerLaunchWei`; annualising one side of
that would make a loss-making format look solvent just for being quick.

The rules come from [Abiogenesis](https://github.com/IlumCI/Abiogenesis), which exists to test which
selection pressures keep producing novelty: frequency-dependent payoff (`--streams`), the poorest-first
reaper (`--reap-poor`), one-point crossover (`--sex`) and an order-2 mutation model (`--learned-mut`).

Our own copies are counted exactly. The rest of the market comes from the DoublePlus aggregator:

```bash
npx luaf persistent --aggregator http://localhost:8081
```

Without it the loop sees only our own launches, which is right where we are the only launchpad on a
chain and too optimistic anywhere else. With it, the count is per **chain**, not per format — a
third-party coin's terms are not in its launch event, so nobody can say which of our formats it
copies. That projection overstates competition on purpose, so the loop retreats from a crowded chain
early rather than finding out by losing money there. Our own copies are subtracted first, so a
launch is never counted twice.

The brief is deliberately **not** part of the format. Breeding carries the terms forward — taxes,
buckets, dividend policy, opening buy, profile — and each launch finds its own subject.

```bash
npx luaf persistent --target-eth 1 --max-launches 20 --population runs/population.json
```

The loop stops on the income target, the launch cap, or when selection has reaped everything: if no
format earns more than a launch costs, continuing would be paying to add copies to a market that
pays nothing for them.

## Configuration

Every setting is an environment variable, and every flag sets one, so `.env` and the command line
are interchangeable. `luaf init` writes the full template. The ones with no sane default:

| Variable | Why it has no default |
| --- | --- |
| `OPENAI_API_KEY` | never defaulted in source |
| `LUAF_TENANT` | picks the deployment to launch into |
| `LUAF_ETH_USD` | sizes the curve's start price, and the factory checks it against its band |
| `LUAF_PRIVATE_KEY` | only read when `LUAF_DRY_RUN=0` |

## The endpoint bundle

Each run writes `runs/<timestamp>-<brief>/endpoint/`: the agent, a server, a Dockerfile, a
`fly.toml`, an OpenAPI document and a README.

- `GET /health` — free.
- `GET /terms` — the x402 payment requirements, so a caller can quote itself.
- `POST /run` — charged. No `X-PAYMENT` header gets **402** and the terms; with one, the payment is
  verified *and settled* through a facilitator before the agent runs. If payment cannot be checked,
  the call is refused rather than served for free.

`X402_DISABLE=1` serves it for nothing. That is for local testing; on a deployed instance it gives
the agent away.

## Licence

See `LICENSE`.
