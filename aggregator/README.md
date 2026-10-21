# aggregator — every launchpad's coins, as inventory

A launchpad earns only from the coins it launched. A terminal earns from every coin on the chain,
whoever launched it — which is why trading terminals out-earn the launchpads they sit on top of.
This service is the data half of that: a multi-chain, multi-launchpad indexer. `AggregatorRouter`
(in `contracts/contracts/v4/`) is the half that charges.

It also answers the question the foundry needs: **how crowded is this kind of launch right now?**

## Sources are configuration, not code

A launchpad is a **source** in a config file, and a new one is added by describing its launch event
rather than by shipping code for it. That is the whole point: a terminal that needs a release to
index a competitor will always be behind the competitor.

Two kinds. `venture` means one of our own stacks, whose ABI we know. `generic` means somebody
else's, described by its launch event — so no third-party ABI is compiled in, and a launchpad that
changes its contracts breaks a line of JSON rather than a build.

```jsonc
// sources.json — copy sources.example.json
{
  "chains": [
    { "chainId": 4663, "label": "Robinhood Chain", "rpcUrl": "https://…", "logChunk": 10000 }
  ],
  "sources": [
    // ours: the ABI is known, so launches, graduations and pool ids are exact
    { "id": "doubleplus", "label": "doubleplus", "kind": "venture",
      "chainId": 4663, "address": "0x…", "startBlock": 128714375 },

    // anyone else's: described, not hard-coded
    { "id": "some-pad", "label": "Some launchpad", "kind": "generic",
      "chainId": 4663, "address": "0x…", "startBlock": 0,
      "launchEvent": "event TokenCreated(address indexed token, address indexed creator, string name, string symbol)",
      "map": { "token": "token", "creator": "creator", "name": "name", "symbol": "symbol" },
      "graduateEvent": "event Graduated(address indexed token)",
      "graduateMap": { "token": "token" } }
  ]
}
```

`${VAR}` in an `rpcUrl` is expanded from the environment, so a keyed endpoint stays out of the file.

**No third-party ABI is hard-coded anywhere in this service, deliberately.** An ABI we have not
verified against the deployed contract is a silent source of wrong data: it would either return
nothing, or decode the wrong argument as the coin. A description that does not match its own event
is rejected at startup, and a log whose shape is not what was described is skipped rather than
guessed at.

## Run it

```bash
npm install
cp sources.example.json sources.json   # then edit it
npm run build && npm start             # or: npm run dev
npm test                               # 30 tests
```

`sources.testnet.json` is committed and ready to run against the live Robinhood Chain testnet
venture stack — public RPC, public addresses, nothing secret:

```bash
SOURCES_FILE=sources.testnet.json npm start
```

| Variable | Default |
| --- | --- |
| `SOURCES_FILE` | `sources.json` |
| `PORT` | 8081 |
| `DB_PATH` | `./data/aggregator.db` |
| `POLL_INTERVAL_MS` | 5000 |
| `WINDOW_DAYS` | 7 |

Each source keeps its own cursor, committed per page, so adding a launchpad backfills only that one,
an interrupted backfill resumes where it stopped, and one unreachable endpoint does not stall the
rest.

## API

| Route | What it gives |
| --- | --- |
| `GET /` and `/terminal` | the terminal page |
| `GET /health` | liveness, plus what has been indexed so far |
| `GET /api/sources` | the chains and launchpads being indexed |
| `GET /api/coins?chainId=&source=&limit=&offset=` | inventory, newest first |
| `GET /api/saturation?windowDays=` | how crowded each chain is |
| `WS /ws` | `coin:launched`, `coin:graduated` |

Indexing starts after the server is listening, so `/health` answers during a long first backfill
instead of looking dead to a platform health check.

## The terminal

`public/terminal.html` is the surface, served by this service at `/`. One file, no build step, no
web fonts and no CDN: it reads only the API above and follows the WebSocket, so anything that can
answer the API can hand out the page, there is no second thing to deploy or keep in step, and it
renders with the machine unplugged. It is read once at startup, so an edit needs a restart.

Catppuccin Mocha, flat: content sits on the page, separated by hairlines and space rather than
wrapped in boxes. Sans for labels and prose, monospace only where figures and addresses need to
line up. Colour carries meaning and nothing else — green is trading, blue is still filling, yellow
is a figure not to trust, red is broken. `service/public/console.html` uses the same tokens; the two
are served by different processes, so the style block is duplicated rather than shared, and both
need editing together.

It shows every coin on every indexed launchpad and chain, whether it is trading or still filling,
and how long it took to reach a market — the turnover selection divides by. The crowding table sits
above it. One reading is stated rather than left in a column: a coin that reached a market in under
ten minutes was usually bought out by whoever launched it, so the turnover figure beside it is
measuring that and not demand.

**Trading from it is not wired up yet**, and the page says so rather than offering a button that
does nothing: there is no `AggregatorRouter` deployment to send a trade to. The contract is the half
that charges — a fee on the input before the swap, with a member launchpad's share paid out of it —
so until it exists on a chain, the terminal brings traffic and collects nothing. Deploy it
(`npm run deploy:aggregator -w contracts`) and that panel is where the trade goes.

## What saturation can and cannot see

This feeds the foundry's selection loop, which divides a format's fee income by the number of live
copies of it. Being straight about the limits matters, because the loop spends money on the answer.

**Exact:** how many coins each launchpad launched in the window, and how many reached an open
market. Read from its own events.

**Not observable:** a third-party coin's *terms* — tax split, dividend policy, the format it is
copying. Those are not in its launch event, and usually not on chain. So rival counts cannot be
keyed to one of our formats for anyone else's coins.

**The projection:** every coin launched on the same chain in the window counts as a rival to every
format on that chain. That is coarse on purpose, and coarse in the safe direction — it overstates
competition, so the foundry retreats from a crowded chain early rather than discovering saturation
by losing money on it. The foundry subtracts its own copies before applying it, so a launch is never
counted twice. Our own coins are additionally counted exactly, by their real format, inside the
foundry.

The graduation rate is reported next to the launch count, because it is the better signal: a chain
minting thousands of coins that never graduate is saturated however high its launch count looks.

**Median time to graduation** is reported per launchpad as well, and it is the measure that makes a
competitor comparable rather than merely countable. Two launchpads graduating the same share of
their coins are not equally good businesses if one takes a week and the other a quarter — Capital II
ch. 16: the annual rate "is always equal to s'n", and its two capitals differ by "900% in the annual
rate" on turnover period alone. The report gives both the median seconds and the turnovers per year
it implies, so our curve can be held against the alternatives on the same axis the foundry already
scores its own formats on.

The median, not the mean: a few coins that sat on a curve for months would drag a mean far enough
that two launchpads could not be told apart. Coins whose timestamps are impossible — a graduation
before its own launch, from a reorg or a bad block read — are dropped rather than counted as speed.

The implied rate is **capped at one turnover a day**, matching the foundry's own fitness. This is not
theoretical: run against the live testnet stack it first reported *181,365 turnovers a year*, off a
median of 174 seconds — a smoke-test coin whose operator filled his own curve in three minutes. The
median seconds is the fact; the rate is an interpretation, and an interpretation that says 181,365 is
wrong.

`fastGraduations` counts graduations completed in under ten minutes. A curve filled in minutes is the
launchpad's operator buying his own coin to the threshold, not demand for it. It is counted and
labelled rather than quietly excluded, because it is a real fact about that launchpad — just not the
one turnover is measuring. Read it beside the graduation rate: a pad with a high rate and a high
fast count is graduating its own coins.

## Not built here

The terminal's front end. A `VITE_BRAND=aggregator` flavor would reuse `web/`'s `Explore`,
`ExploreBoard` and `Token` components over `/api/coins`. It is a UI surface rather than revenue
logic, and shipping a multi-launchpad trading screen whose adapters have not been verified against
live contracts would be worse than not shipping one.
