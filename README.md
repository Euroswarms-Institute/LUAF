# Lucreocracy

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

It runs as a service behind the website, not as a tool. Formerly LUAF.

### The name

A Lucrehulk is a cargo freighter with weapons bolted to it — commercial infrastructure repurposed as
power — flown by a merchant cartel that holds a fleet and a seat in the senate. That is Hilferding's
finance capital and Lenin's financial oligarchy in one image, and it is also the shape of this
business: the launchpad rails are the freighter, the toll is the armament.

From *lucrum* and *-kratia*: rule by money. Which is the thesis of every text this design was
derived from, and the reason the code comments cite them.

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
npm test                        # 7 contract, 117 core, 47 service

cp .env.example .env             # then fill it in
cp service/tenants.example.json tenants.json   # one entry per tenant
npm start                        # the service, on :8080

curl localhost:8080/health                                   # open
curl -H "Authorization: Bearer $TOKEN" localhost:8080/ready   # that tenant's view
```

A dry run still simulates the launch against the real factory, so it fails for the same reasons a
live one would — a paused factory, an ETH/USD price outside the deployed band, a first buy over the
launch-window cap.

## Who it works for, and how far

One process serves every tenant in the grant file. The token decides which: a request may not name
its tenant, because its credential already did and a caller cannot edit their own credential. Each
grant gets its own deployment, run directory, job history, loop and budget.

Three things must line up before anything is signed:

| | |
| --- | --- |
| the server | started with `LUCRE_DRY_RUN=0` and an operator key |
| the grant | `"maySign": true`, and within its launch and spend caps |
| the request | `"live": true` |

Any one missing is a dry run, and a grant can only narrow what the server may do — `maySign` on a
dry-run server is still a dry run. A website bug that forgets the field costs nothing.

The caps are not bureaucracy. One operator key signs for every tenant, so a scope without a budget
is not a narrowing at all: a tenant who can ask for launches without bound can spend our ETH
without bound. So a grant carries `maxLaunches` and `maxSpendWei`, debited **before** the work
starts — two requests in flight must not be checked against the same balance — and refunded only
once we know nothing was signed. On a failure the operator's nonce decides: unmoved means nothing
was broadcast and the reservation comes back; moved, or unreadable, and the spend stays debited,
because the other direction gives away ETH. A loop reserves the gene pool's largest opening buy
times the launches it is allowed, since its later specs do not exist yet, and gives the difference
back when it stops.

The ledger is on disk, so a restart does not hand a tenant a fresh allowance, and a ledger that
cannot be read refuses to spend rather than reading as zero.

### The console

`/console` is the operator's page, served by the service it drives. One console serves every
launchpad, so taking on a tenant does not mean editing a tenant's website.

A grant token is long-lived, ownerless and opens every route, so it belongs to a backend and never
to a browser. A wallet on the grant's `operators` list signs a message naming the launchpad instead,
and gets a session bound to that wallet and that launchpad, lasting `LUCRE_SESSION_HOURS` rather
than forever. That is the shape the agent credential-vault work argues for
([arXiv:2604.23280](https://arxiv.org/pdf/2604.23280)): the weakest holder gets a short, scoped
token, never the standing credential.

The text the wallet signs says what it authorises — launches that spend the operator's ETH, within
the launchpad's own limit — because a generic "sign in" is consent to nothing in particular. The
page builds that text by hand in plain JavaScript, so a test lifts its function out and runs it
against the server's; if the two drift, that test fails rather than every sign-in failing with
"that signature does not match" and nothing saying why.

Revoking a wallet does not wait for its session to expire and does not cost a restart, which would
stop the loop and lose every running job: remove it from the grant file and send `SIGHUP`. Only the
operator lists are taken from the reload, so a running loop cannot find its caps or its chain
rewritten underneath it.

A console session reaches what the grant does, minus `/endpoint-income`, which is a read of an
unrelated payment chain. The list is short on purpose: the real narrowing is the short lifetime and
the allowlist, and the budget is what bounds the damage if a session is stolen. A longer list of
forbidden paths would be theatre.

## Layout

| Path | What it is |
| --- | --- |
| `contracts/contracts/Launcher.sol` | creator of every coin the pipeline launches; forces creator income to the splitter and tags each launch with its `specId` |
| `contracts/contracts/vendor/doubleplus/` | the DoublePlus venture stack, vendored so the Launcher is tested against the contracts it will actually meet. Re-sync with `contracts/scripts/sync-doubleplus.sh` |
| `contracts/contracts/v4/SyndicateRegistry.sol` | who is a member, read live through the factory, so leaving stops the stream for every coin a launchpad ever opened |
| `contracts/contracts/v4/AggregatorRouter.sol` | charges on volume routed through any pool, ours or anyone else's, and pays a member its share |
| `contracts/scripts/deploy-launcher.ts` | reads a `tenant-<slug>-<chain>.json`, deploys a creator `FeeSplitter` and the `Launcher`, writes `launcher-<slug>-<chain>.json` |
| `contracts/scripts/deploy-tenant.ts` | a whole venture stack whose treasury is a tenant `FeeSplitter` |
| `contracts/scripts/deploy-aggregator.ts` | the registry and the router that pays into it |
| `contracts/scripts/preflight.ts` | every read-only check that has to pass before a deploy spends anything |
| `contracts/scripts/verify-deployment.ts` | reads a deployment back off the chain and checks every immutable wire against its artifact |
| `aggregator/src/indexer.ts` | every launchpad on every configured chain, ours and other people's |
| `aggregator/src/saturation.ts` | how crowded each chain is, and how fast coins reach an open market |
| `aggregator/public/terminal.html` | the terminal: every launchpad's coins in one feed, served by the aggregator |
| `keeper/venture-treasury.mjs` | converts fee income to ETH, services a tenant splitter, registers coins with the syndicate |
| `keeper/venture-regime.mjs` | what the market will take: expansion, normal, contraction, and the reserve floor |
| `core/src/spec.ts` | the launch spec: the unit of work, and the genome selection breeds |
| `core/src/pipeline.ts` | one run, end to end |
| `core/src/selection.ts` | frequency-dependent fitness, the reaper, crossover, order-2 mutation |
| `core/src/income.ts` | fee events per coin and per spec |
| `core/src/publish/evm.ts` | salt mining and the launch |
| `core/src/publish/endpoint.ts` | the x402 bundle |
| `service/src/routes.ts` | the HTTP surface the website calls, scoped to the tenant its token names |
| `service/src/tenants.ts` | the grants, and the budget each one spends against |
| `service/src/spend.ts` | what a finished run really cost |
| `service/src/request.ts` | what a caller may ask for, and what the server keeps |
| `service/src/siwe.ts` | console sign-in: a wallet proves itself, and gets a short session instead of the grant token |
| `service/public/console.html` | the operator's page, one file with no build step |
| `core/data/profiles/` | the designer's prompt sets, one directory per profile |

## The parts, and why they are all here

| | Earns | Why it is in this repo |
| --- | --- | --- |
| `Launcher` + `core` + `service` | creator fees and the dev bucket on every coin it opens | the supply |
| tenant `FeeSplitter` | `protocolBps` of everything a tenant's stack earns, enforced by where the fees are addressed | the rent |
| `SyndicateRegistry` + `AggregatorRouter` | a cut of volume routed through any pool, whoever launched the coin | the hedge against launchpads becoming a commodity |
| `aggregator` + its terminal | nothing yet: the router has no deployment to route to | the toll booth needs a road, and the road does not care who launched the coin |
| `keeper` | nothing directly; it is what turns accrued fees into ETH | fees accrue in the coins they were charged in |

The venture contracts these build on are written in the DoublePlus repo and
vendored here read-only (`contracts/contracts/vendor/doubleplus/`, re-pinned by
`contracts/scripts/sync-doubleplus.sh`, which also carries the stack's own test
fixture so the suites here deploy the real contracts rather than an
approximation of them). They are *deployed* from here, by
`contracts/scripts/deploy-tenant.ts`. Anything that reads a deployment artifact
takes its path from the environment rather than guessing a filename, because one
chain can carry several stacks and a tool pointed at the wrong one is wrong
silently.

`DEPLOY.md` is the runbook: what is irreversible, the order, and what to check
after each step.

## Deploying for a tenant

A tenant first needs a venture stack (`contracts/scripts/deploy-tenant.ts`; see
`DEPLOY.md`). Then:

```bash
cd contracts
RPC_URL=… CHAIN_ID=4663 PRIVATE_KEY=0x… \
TENANT_DEPLOYMENT=deployments/tenant-acme-4663.json \
OPERATOR=0x… PROTOCOL_BPS=1000 \
npm run deploy:launcher
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

### Where the fee numbers come from

`core/scripts/launch-batch.mjs` launches real coins across a set of launch formats, graduates each one
and trades it, then reads the income back with the same `collectIncome` the persistent loop uses. It goes
through `publish()` rather than a hand-rolled launch, so it exercises the production path; the designer
is skipped by handing it a `Product` directly, because what a format earns does not depend on the prose
in its description.

```bash
cd core
LUCRE_DRY_RUN=0 LUCRE_PRIVATE_KEY=0x… LUCRE_RPC_URL=… \
LAUNCHER_DEPLOYMENT=../contracts/deployments/launcher-lucre-46630.json \
ETH_USD_8=1000000000000000 npm run launch:batch -- 6 2
```

**What it measures, and what it does not.** Every coin is traded on the same schedule, so the
differences between formats are differences in fee *capture* — how the hook's toll and the founder tax
split under each parameter set — and not differences in demand. Demand is what the aggregator's
market-wide copy counts are for, and it cannot be synthesised. The output is a measured fee-mechanics
table, which is the thing the reaper had no source for; it is not evidence that one format sells better
than another.

Our own copies are counted exactly. The rest of the market comes from the `aggregator` service in this repo:

```bash
curl -X POST -H "Authorization: Bearer $LUCRE_API_TOKEN" \
     -H 'content-type: application/json' -d '{}' localhost:8080/loop/start
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
curl -X POST -H "Authorization: Bearer $LUCRE_API_TOKEN" \
     -H 'content-type: application/json' -d '{}' localhost:8080/loop/start
curl -H "Authorization: Bearer $LUCRE_API_TOKEN" localhost:8080/loop
```

The loop stops on the income target, the launch cap, when selection has reaped everything, or when
the market stops absorbing.

That last one is its own limit. Engels on the anarchy of production: "No one knows how much of his
particular article is coming on the market, nor how much of it will be wanted" — and then "the
markets are glutted, products accumulate, as multitudinous as they are unsaleable", clearing only
"more or less depreciated in value" (Anti-Dühring III.2). A coin launched and still sitting on its
curve is that accumulation: it has tied up its gas and its opening buy and produces nothing until it
graduates. So the loop refuses to launch when too many coins are open at once (`--max-open`,
default 8) or when too few of its launches ever graduate (`--min-graduation`, default 0.1, ignored
until five launches have accumulated). Stopping because the market has had enough is the one thing
the anarchy of production cannot do.

## Configuration

Every setting is an environment variable; there are no flags. `.env.example` is the full template.
The ones with no sane default:

| Variable | Why it has no default |
| --- | --- |
| `OPENAI_API_KEY` | never defaulted in source |
| `LUCRE_TENANT` | picks the deployment to launch into |
| `LUCRE_ETH_USD` | sizes the curve's start price, and the factory checks it against its band |
| `LUCRE_PRIVATE_KEY` | only read when `LUCRE_DRY_RUN=0` |

## The endpoint bundle

Each run writes `runs/<timestamp>-<brief>/endpoint/`: the agent, a server, a Dockerfile, a
`fly.toml`, an OpenAPI document and a README.

- `GET /health` — free.
- `GET /terms` — the x402 payment requirements, so a caller can quote itself.
- `POST /run` — charged. No `X-PAYMENT` header gets **402** and the terms; with one, the payment is
  verified *and settled* through a facilitator before the agent runs. If payment cannot be checked,
  the call is refused rather than served for free.

### Who it pays, and why that is checked

`X402_PAY_TO` must be an address somebody controls **on the payment network**. The creator splitter
is only accepted when `X402_NETWORK` settles on the chain that splitter was deployed to — then it
really is there and the tenant's 90/10 terms hold on endpoint income too.

Otherwise the pipeline refuses. A `FeeSplitter` is a contract, its address comes from a deploy nonce
on one chain, and no private key exists for it anywhere. Naming it as the recipient on Base while it
lives on Robinhood Chain would send every payment to an account with no code and no owner. That is
not a misconfiguration that shows up as an error later; it is a silent burn of the only revenue in
this business that isn't a transfer between traders.

### Measuring it

```bash
curl -H "Authorization: Bearer $LUCRE_API_TOKEN" localhost:8080/endpoint-income
```

Payments are read from the asset's own `Transfer` log, not from a facilitator's API: a settled
transfer is the fact, and a facilitator's record of one is a claim about it.

It reports **unique payers** beside the total, and will not call revenue a market without them.
Capital I ch. 5: "If equivalents are exchanged, no surplus-value results... Circulation, or the
exchange of commodities, begets no value", and "The capitalist class, as a whole, in any country,
cannot over-reach themselves." Money arriving from two addresses over and over is us paying
ourselves or one customer; the same sum from fifty is demand. By default it wants five payers and no
single payer above 60% of the total.

One limit worth stating: with a shared recipient, endpoint revenue **cannot be attributed to a
particular agent**. Every agent pays the same address, so the total is the total. Per-agent
attribution would need a recipient per agent, which is a deploy per agent — not worth it until there
is revenue to attribute.

`X402_DISABLE=1` serves it for nothing. That is for local testing; on a deployed instance it gives
the agent away.

## Licence

See `LICENSE`.
