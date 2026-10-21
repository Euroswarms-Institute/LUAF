#!/usr/bin/env node
/** The foundry's command line.
 *
 *  `init` writes a .env to fill in, `doctor` checks that everything the
 *  pipeline needs is reachable, `run` does one launch, `persistent` loops under
 *  selection, `income` reports what past launches earned, and `profiles` lists
 *  the designer profiles. Every flag maps to an environment variable, as the
 *  Python CLI did, so a flag and a .env entry are interchangeable. */

import { existsSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  baseSpec,
  chainIdForNetwork,
  collectEndpointIncome,
  concentration,
  defaultAbsorptionLimits,
  formatAtomic,
  resolveEndpointConfig,
  bySpec,
  collectIncome,
  listProfiles,
  loadLauncherDeployment,
  loadProfile,
  llmConfig,
  runConfig,
  runOnce,
  runPersistent,
  search,
  formatKey,
  type LaunchSpec,
} from "@lucreocracy/core";

/** Flags that set an environment variable, so .env and the command line are
 *  the same surface. Mirrors the Python CLI's `_apply_cli_config`. */
const FLAG_ENV: Record<string, string> = {
  "--tenant": "LUCRE_TENANT",
  "--chain-id": "LUCRE_CHAIN_ID",
  "--deployment": "LUCRE_LAUNCHER_DEPLOYMENT",
  "--rpc-url": "LUCRE_RPC_URL",
  "--profile": "LUCRE_PROFILE",
  "--topic": "LUCRE_TOPIC",
  "--model": "LUCRE_MODEL",
  "--min-lines": "LUCRE_MIN_LINES",
  "--max-steps": "LUCRE_MAX_STEPS",
  "--out-dir": "LUCRE_OUT_DIR",
  "--eth-usd": "LUCRE_ETH_USD",
  "--first-buy": "LUCRE_FIRST_BUY_ETH",
  "--target-eth": "LUCRE_TARGET_ETH",
  "--max-launches": "LUCRE_MAX_LAUNCHES",
  "--population": "LUCRE_POPULATION",
  "--aggregator": "LUCRE_AGGREGATOR_URL",
  "--x402-rpc": "X402_RPC_URL",
  "--pay-to": "X402_PAY_TO",
  "--max-open": "LUCRE_MAX_OPEN",
  "--min-graduation": "LUCRE_MIN_GRADUATION_RATE",
};

interface Args {
  command: string;
  flags: Set<string>;
}

function parseArgs(argv: string[]): Args {
  const command = argv[0] ?? "help";
  const flags = new Set<string>();
  for (let i = 1; i < argv.length; i++) {
    const a = argv[i]!;
    if (!a.startsWith("--")) continue;
    const [name, inline] = a.includes("=") ? [a.slice(0, a.indexOf("=")), a.slice(a.indexOf("=") + 1)] : [a, undefined];
    const env = FLAG_ENV[name];
    if (env) {
      const value = inline ?? argv[++i];
      if (value === undefined) fail(`${name} needs a value`);
      process.env[env] = value;
      continue;
    }
    flags.add(name);
  }
  // Boolean flags, resolved after the loop so --no-dry-run always wins.
  if (flags.has("--dry-run")) process.env.LUCRE_DRY_RUN = "1";
  if (flags.has("--no-dry-run")) process.env.LUCRE_DRY_RUN = "0";
  return { command, flags };
}

function fail(msg: string): never {
  console.error(msg);
  process.exit(1);
}

function env(name: string, fallback?: string): string | undefined {
  const v = process.env[name]?.trim();
  return v === "" || v === undefined ? fallback : v;
}

/** ETH/USD with 8 decimals. Supplied rather than fetched: it sizes the curve's
 *  start price, the factory checks it against its deployed band, and a wrong
 *  automatic value would open a curve at the wrong price. */
function ethUsd8(): bigint {
  const v = env("LUCRE_ETH_USD");
  if (!v) fail("set --eth-usd (or LUCRE_ETH_USD), e.g. --eth-usd 2500");
  if (!/^\d+(\.\d{1,8})?$/.test(v)) fail(`--eth-usd ${v}: expected a dollar amount like 2500`);
  const [whole = "0", frac = ""] = v.split(".");
  return BigInt(whole) * 10n ** 8n + BigInt(frac.padEnd(8, "0"));
}

function parseEth(v: string | undefined, fallback = 0n): bigint {
  if (!v) return fallback;
  if (!/^\d+(\.\d{1,18})?$/.test(v)) fail(`expected an ETH amount like 0.01, got ${v}`);
  const [whole = "0", frac = ""] = v.split(".");
  return BigInt(whole) * 10n ** 18n + BigInt(frac.padEnd(18, "0"));
}

function specFromEnv(tenant: string, chainId: number): LaunchSpec {
  return baseSpec({
    profile: env("LUCRE_PROFILE", "crypto")!,
    topic: env("LUCRE_TOPIC", "")!,
    tenant,
    chainId,
    firstBuyWei: parseEth(env("LUCRE_FIRST_BUY_ETH")),
  });
}

const ENV_TEMPLATE = `# The foundry's configuration. Every value here can also be passed as a flag.

# --- the model -------------------------------------------------------------
# An OpenAI-compatible endpoint. Never commit a real key.
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com/v1
LUCRE_MODEL=gpt-5.1

# --- what to launch into ---------------------------------------------------
# The tenant slug and chain pick contracts/deployments/launcher-<tenant>-<chain>.json
LUCRE_TENANT=
LUCRE_CHAIN_ID=46630
LUCRE_RPC_URL=https://rpc.testnet.chain.robinhood.com

# --- signing ---------------------------------------------------------------
# Dry run simulates every launch against the real factory and signs nothing.
LUCRE_DRY_RUN=1
# The operator wallet. Only read when LUCRE_DRY_RUN=0.
LUCRE_PRIVATE_KEY=

# --- the design ------------------------------------------------------------
LUCRE_PROFILE=crypto
# Leave the topic blank to have one generated from the profile.
LUCRE_TOPIC=
LUCRE_MIN_LINES=400
LUCRE_MAX_STEPS=3
LUCRE_OUT_DIR=runs

# --- launch terms ----------------------------------------------------------
# ETH/USD in dollars. It sizes the curve's start price and must sit inside the
# band the factory was deployed with.
LUCRE_ETH_USD=
# The operator's opening buy. Clamped to the factory's launch-window cap.
LUCRE_FIRST_BUY_ETH=0

# --- the loop --------------------------------------------------------------
LUCRE_TARGET_ETH=1
LUCRE_MAX_LAUNCHES=10
LUCRE_POPULATION=runs/population.json
# The aggregator tells selection how crowded the chain is. Without it the loop
# counts only our own copies and understates the competition.
LUCRE_AGGREGATOR_URL=
# Stop adding supply when the market stops taking it: at most this many coins
# left sitting on the curve at once, and at least this share of launches
# reaching an open market.
LUCRE_MAX_OPEN=8
LUCRE_MIN_GRADUATION_RATE=0.1

# --- the paid endpoint -----------------------------------------------------
# 0.01 USDC on Base by default.
X402_PRICE_ATOMIC=10000
X402_NETWORK=base
X402_ASSET=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
X402_FACILITATOR=https://x402.org/facilitator
# Who the endpoints pay. REQUIRED unless X402_NETWORK settles on the same
# chain as the launchpad: the creator splitter is a contract on the launch
# chain, no private key exists for a contract address, and paying it on
# another chain would burn every payment. Set an address you control.
X402_PAY_TO=
# RPC for the payment chain (not the launch chain), for lucre endpoint-income.
X402_RPC_URL=
`;

function cmdInit(): void {
  const path = resolve(process.cwd(), ".env");
  if (existsSync(path)) {
    console.log(`.env already exists at ${path}; leaving it alone`);
    return;
  }
  writeFileSync(path, ENV_TEMPLATE, "utf8");
  console.log(`wrote ${path}`);
  console.log("Fill in OPENAI_API_KEY, LUCRE_TENANT and LUCRE_ETH_USD, then run: luaf doctor");
}

async function cmdDoctor(): Promise<void> {
  let bad = 0;
  const ok = (label: string, detail = "") => console.log(`  ok    ${label}${detail ? `  ${detail}` : ""}`);
  const no = (label: string, why: string) => {
    bad++;
    console.log(`  FAIL  ${label}  ${why}`);
  };

  console.log("model");
  try {
    const cfg = llmConfig();
    ok("config", `${cfg.model} at ${cfg.baseUrl}`);
    const res = await fetch(`${cfg.baseUrl}/models`, {
      headers: { authorization: `Bearer ${cfg.apiKey}` },
      signal: AbortSignal.timeout(20_000),
    });
    if (res.ok) ok("endpoint reachable");
    else no("endpoint", `HTTP ${res.status}`);
  } catch (e) {
    no("config", (e as Error).message);
  }

  console.log("profiles");
  try {
    const profiles = listProfiles();
    ok("found", `${profiles.length} profiles`);
    const want = env("LUCRE_PROFILE", "crypto")!;
    loadProfile(want);
    ok("selected", want);
  } catch (e) {
    no("profiles", (e as Error).message);
  }

  console.log("research");
  const snippets = await search("autonomous agent revenue", { maxResults: 3 });
  if (snippets.length) ok("search", `${snippets.length} results`);
  else console.log("  warn  search returned nothing; runs will design without context");

  console.log("chain");
  try {
    const run = runConfig();
    ok("run config", run.dryRun ? "dry run" : "LIVE: launches will be signed");
    const dep = loadLauncherDeployment();
    ok("deployment", `tenant ${dep.tenant} on chain ${dep.chainId}`);
    ok("launcher", dep.contracts.launcher);
    ok("factory", dep.contracts.factory);
    const res = await fetch(run.rpcUrl, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_chainId", params: [] }),
      signal: AbortSignal.timeout(20_000),
    });
    const json = (await res.json()) as { result?: string };
    const served = json.result ? Number(BigInt(json.result)) : 0;
    if (served === dep.chainId) ok("rpc", `chain ${served}`);
    else no("rpc", `serves chain ${served}, the deployment is for ${dep.chainId}`);
  } catch (e) {
    no("chain", (e as Error).message);
  }

  console.log(bad === 0 ? "\nall checks passed" : `\n${bad} check(s) failed`);
  if (bad) process.exit(1);
}

async function cmdRun(args: Args): Promise<void> {
  const run = runConfig();
  const dep = loadLauncherDeployment();
  const spec = specFromEnv(dep.tenant, dep.chainId);
  const result = await runOnce({
    spec,
    dep,
    llm: llmConfig(),
    run,
    ethUsd8: args.flags.has("--design-only") ? 0n : ethUsd8(),
    designOnly: args.flags.has("--design-only"),
    log: (l) => console.log(l),
  });
  console.log("\n--- done");
  console.log(`agent:   ${result.product.name} (${result.product.ticker}), ${result.lines} lines`);
  console.log(`spec:    ${result.specId}`);
  if (result.publish) {
    console.log(`coin:    ${result.publish.token}${result.publish.dryRun ? " (dry run, nothing signed)" : ""}`);
    if (result.publish.txHash) console.log(`tx:      ${result.publish.txHash}`);
  }
  console.log(`bundle:  ${result.bundle?.dir}`);
  console.log(`run dir: ${result.dir}`);
}

async function cmdPersistent(): Promise<void> {
  const run = runConfig();
  const dep = loadLauncherDeployment();
  const seed = specFromEnv(dep.tenant, dep.chainId);
  const result = await runPersistent({
    seed,
    dep,
    llm: llmConfig(),
    run,
    ethUsd8: ethUsd8(),
    targetEthWei: parseEth(env("LUCRE_TARGET_ETH"), 10n ** 18n),
    maxLaunches: Number(env("LUCRE_MAX_LAUNCHES", "10")),
    populationPath: env("LUCRE_POPULATION", "runs/population.json"),
    aggregatorUrl: env("LUCRE_AGGREGATOR_URL"),
    absorption: {
      ...defaultAbsorptionLimits,
      ...(env("LUCRE_MAX_OPEN") ? { maxOpen: Number(env("LUCRE_MAX_OPEN")) } : {}),
      ...(env("LUCRE_MIN_GRADUATION_RATE") ? { minGraduationRate: Number(env("LUCRE_MIN_GRADUATION_RATE")) } : {}),
    },
    log: (l) => console.log(l),
  });
  console.log("\n--- done");
  console.log(`stopped:  ${result.stopped}`);
  console.log(`launches: ${result.launches} (${result.failures} failed), ${result.epochs} epochs`);
  console.log(`income:   ${result.incomeEthWei} wei`);
  console.log(`specs:    ${result.population.members.length} alive`);
  if (result.absorption) {
    const a = result.absorption;
    const rate = a.graduationRate === null ? "n/a" : `${(a.graduationRate * 100).toFixed(1)}%`;
    console.log(`market:   ${a.open} coins still on the curve, ${a.graduated}/${a.launched} graduated (${rate})`);
  }
  if (result.stopped === "glutted") {
    console.log("\nStopped because the market stopped absorbing, not because it ran out of work.");
  }
}

async function cmdIncome(): Promise<void> {
  const run = runConfig();
  const dep = loadLauncherDeployment();
  const rows = await collectIncome(dep, run.rpcUrl);
  if (rows.length === 0) {
    console.log("no launches found for this launcher yet");
    return;
  }
  console.log(`${rows.length} coin(s) from launcher ${dep.contracts.launcher}\n`);
  for (const r of rows) {
    console.log(`${r.token}  spec ${r.specId.slice(0, 10)}…  ${r.graduated ? "graduated" : "on curve"}`);
    console.log(`  curve: creator ${r.curveCreatorWei} wei, platform ${r.curvePlatformWei} wei`);
    if (r.graduated) {
      console.log(`  pool:  platform ${r.poolPlatformPairWei} wei, dev ${r.poolDevPairWei} wei, ${r.swaps} charged swaps`);
      if (r.poolPlatformCoin || r.poolDevCoin) {
        console.log(`  pool (in the coin, not ETH): platform ${r.poolPlatformCoin}, dev ${r.poolDevCoin}`);
      }
    }
    const days = (secs: number) => (secs / 86_400).toFixed(1);
    console.log(
      r.turnoverComplete
        ? `  turnover: ${days(r.turnoverSecs)} days, complete`
        : `  turnover: ${days(r.turnoverSecs)} days and still open (on the curve)`,
    );
    console.log(`  total: ${r.totalEthWei} wei of ETH`);
  }
  let total = 0n;
  console.log("\nper spec:");
  for (const [, agg] of bySpec(rows)) {
    total += agg.totalEthWei;
    const n = agg.meanTurnoverSecs > 0 ? 31_557_600 / agg.meanTurnoverSecs : 1;
    console.log(
      `  ${agg.specId.slice(0, 10)}\u2026  ${agg.tokens.length} coin(s), ${agg.graduated} graduated, ` +
        `${agg.totalEthWei} wei, mean turnover ${(agg.meanTurnoverSecs / 86_400).toFixed(1)}d ` +
        `(${n.toFixed(1)} per year)`,
    );
  }
  console.log(`\ntotal ETH income: ${total} wei`);
}

async function cmdEndpointIncome(): Promise<void> {
  const dep = loadLauncherDeployment();
  const cfg = resolveEndpointConfig({ splitter: dep.contracts.creatorSplitter, launchChainId: dep.chainId });
  const chainId = cfg.chainId ?? chainIdForNetwork(cfg.network);
  if (chainId === null) fail(`X402_NETWORK=${cfg.network} is not a network this build can resolve to a chain id`);
  const rpcUrl = env("X402_RPC_URL");
  if (!rpcUrl) fail(`set --x402-rpc (or X402_RPC_URL): payments settle on chain ${chainId}, not the launch chain`);

  const income = await collectEndpointIncome({
    rpcUrl,
    chainId,
    asset: cfg.asset as `0x${string}`,
    payTo: cfg.payTo as `0x${string}`,
    fromBlock: env("X402_FROM_BLOCK") ? BigInt(env("X402_FROM_BLOCK")!) : undefined,
  });
  const verdict = concentration(income);
  const decimals = Number(env("X402_ASSET_DECIMALS", "6"));

  console.log(`endpoint revenue, paid to ${income.payTo} on chain ${income.chainId}`);
  console.log(`  asset:    ${income.asset}`);
  console.log(`  received: ${formatAtomic(income.totalAtomic, decimals)} over ${income.payments} payment(s)`);
  console.log(`  payers:   ${income.uniquePayers}`);
  if (verdict.topPayerShare !== null) {
    console.log(`  largest:  ${formatAtomic(income.largestPayerAtomic, decimals)} (${(verdict.topPayerShare * 100).toFixed(0)}%)`);
  }
  console.log(`  verdict:  ${verdict.isMarket ? "a market" : "not yet a market"} \u2014 ${verdict.note}`);
  console.log(
    "\nThis is the only income here that is not a transfer between traders. Curve fees, pool tolls\n" +
      "and router fees are all shares of value produced elsewhere; `lucre income` reports those.",
  );
}

function cmdProfiles(): void {
  for (const id of listProfiles()) {
    const p = loadProfile(id);
    console.log(`${id.padEnd(34)} ${p.displayName}`);
  }
}

function cmdFormat(): void {
  const dep = (() => {
    try {
      return loadLauncherDeployment();
    } catch {
      return undefined;
    }
  })();
  const spec = specFromEnv(dep?.tenant ?? "none", dep?.chainId ?? 0);
  console.log(formatKey(spec));
}

const HELP = `lucre — design an autonomous unit, launch it as a coin, charge for its endpoint

  lucre init                  write a .env to fill in
  lucre doctor                check the model, profiles, research and chain
  lucre run [--design-only]   one pass: brief, research, design, validate, launch, bundle
  lucre persistent            loop under selection until the income target or the launch cap
  lucre income                what past launches have earned, per coin and per spec
  lucre endpoint-income       what the paid endpoints have been paid, and by how many
  lucre profiles              list the designer profiles
  lucre format                print the format key of the configured spec

Common flags (each sets the matching environment variable):
  --tenant <slug>      --chain-id <id>        --deployment <path>
  --rpc-url <url>      --profile <id>         --topic "<brief>"
  --eth-usd <dollars>  --first-buy <eth>      --min-lines <n>
  --max-steps <n>      --out-dir <path>       --model <id>
  --target-eth <eth>   --max-launches <n>     --population <path>
  --aggregator <url>   --max-open <n>         --min-graduation <0..1>
  --x402-rpc <url>     --pay-to <address>
  --dry-run | --no-dry-run

Nothing is signed unless --no-dry-run is passed. A dry run still simulates every
launch against the real factory, so it fails for the same reasons a live one would.`;

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  switch (args.command) {
    case "init":
      return cmdInit();
    case "doctor":
      return cmdDoctor();
    case "run":
      return cmdRun(args);
    case "persistent":
      return cmdPersistent();
    case "income":
      return cmdIncome();
    case "endpoint-income":
      return cmdEndpointIncome();
    case "profiles":
      return cmdProfiles();
    case "format":
      return cmdFormat();
    case "help":
    case "--help":
    case "-h":
      console.log(HELP);
      return;
    default:
      console.error(`unknown command ${JSON.stringify(args.command)}\n`);
      console.log(HELP);
      process.exit(1);
  }
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
