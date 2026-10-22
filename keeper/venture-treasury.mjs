// Venture treasury keeper: turns fee income into ETH.
//
// The hook takes its fee on the swap's unspecified currency, so the treasury
// collects launched coins as well as WETH. Launched coins all fall together in
// a crash, so the treasury's coin balances are sold to ETH on a schedule rather
// than held. Every run:
//   1) tenant stacks (the deployment has a `tenant` block): pull the factory's
//      fee ledger into the FeeSplitter, redeem any ERC-6909 claims the hook
//      minted to it, and split every asset it holds between tenant and protocol;
//   2) sell a share of each graduated coin the keeper wallet holds, set by the
//      market regime (see venture-regime.mjs): fastest in a contraction,
//      because a launched coin is the fictitious capital that Capital III
//      ch. 30 says is "enormously reduced in times of crisis"
//      through the venture router (coin -> WETH -> ETH), with a slippage floor
//      taken from a static-call quote;
//   3) unwrap any WETH the wallet holds;
//   4) register newly graduated coins with the syndicate registry, if this
//      stack is a member, so the shared router starts paying us for volume on
//      them. A registry nobody populates pays nobody.
//
// Step 2 sells the wallet's own coins, so it converts fees only where the
// keeper wallet is the treasury (doubleplus) or the protocol's splitter
// recipient (tenants). The flywheel already funds itself from ETH balance.
//
// Env:
//   KEEPER_PRIVATE_KEY  (required) treasury/keeper wallet; pays gas
//   RPC_URL, CHAIN_ID, DEPLOYMENT_FILE, LOG_CHUNK  as in venture-config.mjs
//   MIN_SELL_ETH        (default 0.002) skip a sale quoted below this
//   MAX_SELL_BPS        overrides the regime's share; unset lets the regime
//                       decide (2000 expansion / 2500 normal / 6000 contraction)
//   VOLUME_WINDOW_BLOCKS (default 200000) blocks of routed volume that make up
//                       one regime observation
//   AGGREGATOR_DEPLOYMENT  the aggregator deployment JSON, written by
//                       contracts/scripts/deploy-aggregator.ts (default
//                       ../contracts/deployments/aggregator-<chainId>.json).
//                       Absent, or this stack not being a member, skips step 4.
//   REGIME_FILE         where the trailing series lives (default
//                       keeper/state/regime.json). It is a trailing series, so
//                       it has to survive between runs: a runner that keeps no
//                       disk must commit it or point this at storage that
//                       persists, or every run reads an empty history and
//                       calls the market normal. The default is a committed
//                       path rather than runs/, which is gitignored, precisely
//                       so the scheduled job can commit it.
//   (legacy) MAX_SELL_BPS  most of one coin holding sold per run,
//                       so a thin pool is not dumped in one block
//   SLIPPAGE_BPS        (default 300) floor below the quote
//   MAX_SPEND_ETH       (default 0.01) gas this run may spend
//   CONFIRMATIONS       (default 2)
//   DRY_RUN             log intended actions without sending txs
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { ethers } from "ethers";

import { DRY_RUN, envEth, envInt, KEY, loadDeployment, LOG_CHUNK, RPC } from "./venture-config.mjs";
import { appendObservation, classify, policyFor } from "./venture-regime.mjs";

const { dep, depPath } = loadDeployment();
const MIN_SELL_WEI = envEth("MIN_SELL_ETH", "0.002");
// Unset means the regime decides. An explicit value overrides it, for a
// manual sweep.
const MAX_SELL_BPS_OVERRIDE = process.env.MAX_SELL_BPS?.trim()
  ? BigInt(envInt("MAX_SELL_BPS", 2500, 1, 10_000))
  : null;
const VOLUME_WINDOW_BLOCKS = envInt("VOLUME_WINDOW_BLOCKS", 200_000, 1, 50_000_000);
const here = dirname(fileURLToPath(import.meta.url));
const REGIME_FILE = process.env.REGIME_FILE?.trim() || join(here, "state/regime.json");
const SLIPPAGE_BPS = BigInt(envInt("SLIPPAGE_BPS", 300, 0, 5_000));
const MAX_SPEND_WEI = envEth("MAX_SPEND_ETH", "0.01");
// At least 1. wait(0) resolves without waiting for inclusion, so every later
// transaction in the run is built on a nonce the previous one already took and
// fails with "nonce has already been used" -- which it did, on three of eleven,
// the first time this was run with 0.
const CONFIRMATIONS = envInt("CONFIRMATIONS", 2, 1, 64);
// Gas limit as a multiple of the estimate for the router's V4 path. 12,500 bps
// = 1.25x. The measured shortfall was about 6%; see withHeadroom below for why
// the estimate is not a limit the transaction can live at.
const GAS_HEADROOM_BPS = BigInt(envInt("GAS_HEADROOM_BPS", 12_500, 10_000, 30_000));

const ZERO = ethers.ZeroAddress;
const ZERO_POOL = ethers.ZeroHash;

const FACTORY_ABI = [
  "function totalTokens() view returns (uint256)",
  "function allTokens(uint256) view returns (address)",
  "function feesAccrued(address) view returns (uint256)",
  "function listings(address) view returns (address creator, address pair, uint16 taxBps, uint64 createdAt, bytes32 poolId)",
];
const ROUTER_ABI = [
  "function sell(address coin, uint256 amountIn, bytes v3PathReverse, uint256 minEthOut) returns (uint256)",
  "event Routed(address indexed trader, address indexed coin, bool isBuy, uint256 ethIn, uint256 ethOut)",
];
const ERC20_ABI = [
  "function balanceOf(address) view returns (uint256)",
  "function allowance(address,address) view returns (uint256)",
  "function approve(address,uint256) returns (bool)",
];
const WETH_ABI = [...ERC20_ABI, "function withdraw(uint256)"];
const REGISTRY_ABI = [
  "function beneficiaryOfFactory(address) view returns (address)",
  "function factoryOfCoin(address) view returns (address)",
  "function registerCoin(address factory, address coin)",
];
const SPLITTER_ABI = [
  "function pullFactory(address) returns (uint256)",
  "function redeem6909(address currency, uint256 amount)",
  "function split(address asset) returns (uint256 toTenant, uint256 toProtocol)",
  "function splittable(address asset) view returns (uint256)",
];
const PM_ABI = ["function balanceOf(address owner, uint256 id) view returns (uint256)"];

const stats = { sent: 0, spentWei: 0n, splits: 0, redeemed: 0, registered: 0, sold: 0, soldEthWei: 0n, unwrappedWei: 0n, skipped: 0, failed: 0, stoppedAtCap: false };
class SpendCap extends Error {}
async function send(label, fn) {
  if (DRY_RUN) { console.log(`[dry-run] ${label}`); return null; }
  if (stats.spentWei >= MAX_SPEND_WEI) { stats.stoppedAtCap = true; throw new SpendCap(label); }
  const rc = await (await fn()).wait(CONFIRMATIONS);
  stats.sent++;
  stats.spentWei += (rc?.gasUsed ?? 0n) * (rc?.gasPrice ?? rc?.effectiveGasPrice ?? 0n);
  return rc;
}

const provider = new ethers.JsonRpcProvider(RPC);
const wallet = new ethers.Wallet(KEY, provider);
const factory = new ethers.Contract(dep.contracts.factory, FACTORY_ABI, wallet);
const router = new ethers.Contract(dep.contracts.router, ROUTER_ABI, wallet);
const weth = new ethers.Contract(dep.contracts.weth, WETH_ABI, wallet);

async function coins() {
  const n = Number(await factory.totalTokens());
  const out = [];
  for (let i = 0; i < n; i++) {
    const coin = await factory.allTokens(i);
    const l = await factory.listings(coin);
    out.push({ coin, graduated: l.poolId !== ZERO_POOL });
  }
  return out;
}

/** Tenant stacks: everything owed to the splitter, split between both sides. */
async function serviceSplitter(all) {
  const splitter = new ethers.Contract(dep.tenant.splitter, SPLITTER_ABI, wallet);
  if ((await factory.feesAccrued(dep.tenant.splitter)) > 0n) {
    await send("splitter.pullFactory", () => splitter.pullFactory(dep.contracts.factory));
  }
  const pm = new ethers.Contract(dep.contracts.poolManager, PM_ABI, provider);
  const assets = [dep.contracts.weth, ...all.filter((c) => c.graduated).map((c) => c.coin)];
  for (const asset of assets) {
    const claims = await pm.balanceOf(dep.tenant.splitter, BigInt(asset));
    if (claims > 0n) {
      // redeem6909 drives PoolManager.unlock as well, so it is the same
      // nested-call shape as a router trade and needs the same headroom.
      const ov = await withHeadroom(splitter.redeem6909, [asset, claims]);
      await send(`splitter.redeem6909 ${asset} ${claims}`, () => splitter.redeem6909(asset, claims, ov));
      stats.redeemed++;
    }
  }
  for (const asset of [ZERO, ...assets]) {
    if ((await splitter.splittable(asset)) === 0n) continue;
    await send(`splitter.split ${asset}`, () => splitter.split(asset));
    stats.splits++;
  }
}

/** A gas limit with headroom over the estimate, for the router's V4 path.
 *
 *  Not belt and braces: a VentureRouter trade at exactly its own estimated gas
 *  limit runs out of gas, while the same trade with more succeeds and uses
 *  *less* than the estimate. Measured on a fork of 46630 against a freshly
 *  graduated pool: estimateGas said 849,368, that limit failed, 900,000
 *  succeeded and consumed 785,359.
 *
 *  The cause is EIP-150. The trade runs inside PoolManager.unlock, so the work
 *  happens in a nested call, which may be given at most 63/64 of the gas
 *  remaining at the call. With a tight limit the inner frame is starved even
 *  though the total would have been enough, and how much gas is actually
 *  consumed therefore depends on the limit it was sent with -- which is exactly
 *  the case estimateGas cannot measure, because it binary-searches on the total.
 *
 *  This matters most at the moment it costs most: the first trade on a newly
 *  graduated pool needs the most gas, and the keeper's first sale of a coin's
 *  fee income is often that trade. Without the headroom the keeper fails
 *  precisely when fees first exist. */
async function withHeadroom(method, args, overrides = {}) {
  // A dry run sends nothing, and estimating can revert for reasons a dry run
  // deliberately does not resolve -- an approval it did not make, a claim it did
  // not redeem. Estimating anyway would turn "nothing happened" into a crash.
  if (DRY_RUN) return overrides;
  const est = await method.estimateGas(...args, overrides);
  return { ...overrides, gasLimit: (est * GAS_HEADROOM_BPS) / 10_000n };
}

/** Sell part of one coin holding to ETH, floored at the quote less slippage. */
async function sellCoin(coin, sellBps) {
  const token = new ethers.Contract(coin, ERC20_ABI, wallet);
  const held = await token.balanceOf(wallet.address);
  const amount = (held * sellBps) / 10_000n;
  if (amount === 0n) return;
  const routerAddr = dep.contracts.router;
  if ((await token.allowance(wallet.address, routerAddr)) < amount) {
    await send(`approve ${coin}`, () => token.approve(routerAddr, ethers.MaxUint256));
    if (DRY_RUN) { stats.skipped++; return; } // the quote needs the approval
  }
  let quote;
  try {
    quote = await router.sell.staticCall(coin, amount, "0x", 0n);
  } catch (e) {
    console.warn(`quote failed for ${coin}: ${e.shortMessage ?? e.message}`);
    stats.failed++;
    return;
  }
  if (quote < MIN_SELL_WEI) { stats.skipped++; return; }
  const minOut = (quote * (10_000n - SLIPPAGE_BPS)) / 10_000n;
  const overrides = await withHeadroom(router.sell, [coin, amount, "0x", minOut]);
  await send(`sell ${coin} ${amount} -> >=${ethers.formatEther(minOut)} ETH`, () => router.sell(coin, amount, "0x", minOut, overrides));
  stats.sold++;
  stats.soldEthWei += quote;
}

/** Routed volume over the recent window: the "credit melts away" signal.
 *  Bounded to VOLUME_WINDOW_BLOCKS so a long-lived factory does not re-scan
 *  its whole history every six hours. */
async function routedVolumeWei(head) {
  const from = Math.max(Number(dep.startBlock ?? 0), head - VOLUME_WINDOW_BLOCKS);
  const topic = router.interface.getEvent("Routed").topicHash;
  let total = 0n;
  for (let start = from; start <= head; start += LOG_CHUNK) {
    const end = Math.min(start + LOG_CHUNK - 1, head);
    const logs = await provider.getLogs({ address: dep.contracts.router, topics: [topic], fromBlock: start, toBlock: end });
    for (const l of logs) {
      const a = router.interface.parseLog(l).args;
      total += a.ethIn + a.ethOut;
    }
  }
  return total;
}

/** What one whole unit of each held coin would fetch, per coin.
 *  Per coin rather than as a total, so the reading survives the basket
 *  changing as coins graduate and fees arrive. */
async function quoteHeldCoins(all) {
  const out = {};
  for (const { coin, graduated } of all) {
    if (!graduated) continue;
    const token = new ethers.Contract(coin, ERC20_ABI, provider);
    const held = await token.balanceOf(wallet.address);
    if (held === 0n) continue;
    try {
      // A whole token, so the number is a price and not a function of how
      // much happens to be held.
      const quote = await router.sell.staticCall(coin, 10n ** 18n, "0x", 0n);
      if (quote > 0n) out[coin.toLowerCase()] = quote.toString();
    } catch {
      // A pool too thin to quote one token tells us nothing; leaving the coin
      // out is better than recording a zero that would read as a crash.
    }
  }
  return out;
}

/** Load the aggregator deployment for this chain, or null when there is none. */
function loadAggregator() {
  const explicit = process.env.AGGREGATOR_DEPLOYMENT?.trim();
  const path = explicit || join(here, `../contracts/deployments/aggregator-${Number(dep.chainId)}.json`);
  if (!existsSync(path)) return null;
  try {
    const a = JSON.parse(readFileSync(path, "utf8"));
    if (Number(a.chainId) !== Number(dep.chainId)) {
      console.warn(`${path} is for chain ${a.chainId}, not ${dep.chainId}; skipping registration`);
      return null;
    }
    return a?.contracts?.registry ? a : null;
  } catch (e) {
    console.warn(`could not read ${path}: ${e.message}`);
    return null;
  }
}

/** Register graduated coins so the shared router pays this stack for their
 *  volume. Skipped silently when the stack is not a member: registerCoin
 *  would revert, and not being in the syndicate is a choice, not a fault. */
async function registerCoins(all, agg) {
  const registry = new ethers.Contract(agg.contracts.registry, REGISTRY_ABI, wallet);
  const beneficiary = await registry.beneficiaryOfFactory(dep.contracts.factory);
  if (beneficiary === ZERO) {
    console.log(`not a syndicate member on ${agg.contracts.registry}; skipping registration`);
    return;
  }
  for (const { coin, graduated } of all) {
    // Only graduated coins trade through the router, so only they can earn.
    if (!graduated) continue;
    if ((await registry.factoryOfCoin(coin)) !== ZERO) continue;
    await send(`registry.registerCoin ${coin}`, () => registry.registerCoin(dep.contracts.factory, coin));
    stats.registered++;
  }
}

function readRegime(all) {
  return (async () => {
    let series = [];
    try {
      if (existsSync(REGIME_FILE)) series = JSON.parse(readFileSync(REGIME_FILE, "utf8")).series ?? [];
    } catch (e) {
      console.warn(`regime history unreadable (${e.message}); starting a new one`);
    }
    const head = await provider.getBlockNumber();
    const observation = {
      at: Math.floor(Date.now() / 1000),
      block: head,
      volumeWei: (await routedVolumeWei(head)).toString(),
      coins: await quoteHeldCoins(all),
    };
    const verdict = classify(series, observation);
    return { ...verdict, observation, series };
  })();
}

function writeRegime(series, observation) {
  if (DRY_RUN) return;
  try {
    mkdirSync(dirname(REGIME_FILE), { recursive: true });
    writeFileSync(REGIME_FILE, JSON.stringify({ series: appendObservation(series, observation) }, null, 2));
  } catch (e) {
    console.warn(`could not write ${REGIME_FILE}: ${e.message}`);
  }
}

async function main() {
  const net = await provider.getNetwork();
  if (Number(net.chainId) !== Number(dep.chainId)) {
    console.error(`chain mismatch: RPC is ${net.chainId}, ${depPath} is for ${dep.chainId}.`);
    process.exit(1);
  }
  const all = await coins();
  const { regime, reasons, observation, series } = await readRegime(all);
  const policy = policyFor(regime);
  const sellBps = MAX_SELL_BPS_OVERRIDE ?? BigInt(policy.sellBps);
  console.log(`regime: ${regime} (${reasons.join("; ")}) -> selling ${sellBps} bps per coin`);

  const agg = loadAggregator();
  try {
    if (agg) {
      try { await registerCoins(all, agg); } catch (e) {
        if (e instanceof SpendCap) throw e;
        console.warn(`registration failed: ${e.shortMessage ?? e.message}`);
        stats.failed++;
      }
    }
    if (dep.tenant?.splitter) await serviceSplitter(all);
    for (const { coin, graduated } of all) {
      if (!graduated) continue;
      try { await sellCoin(coin, sellBps); } catch (e) {
        if (e instanceof SpendCap) throw e;
        console.warn(`sell failed for ${coin}: ${e.shortMessage ?? e.message}`);
        stats.failed++;
      }
    }
    const wrapped = await weth.balanceOf(wallet.address);
    if (wrapped > 0n) {
      await send(`unwrap ${ethers.formatEther(wrapped)} WETH`, () => weth.withdraw(wrapped));
      stats.unwrappedWei += wrapped;
    }
  } catch (e) {
    if (!(e instanceof SpendCap)) throw e;
    console.warn(`spend cap reached at ${e.message}`);
  }
  // Written even when the run hit its spend cap: the observation is a
  // measurement of the market, not a record of what we managed to do.
  writeRegime(series, observation);

  const balance = await provider.getBalance(wallet.address);
  console.log(JSON.stringify({
    keeper: "venture-treasury", chain: Number(dep.chainId), tenant: dep.tenant?.slug ?? null,
    regime, sellBps: Number(sellBps), coins: all.length, ...stats,
    spentWei: stats.spentWei.toString(), soldEthWei: stats.soldEthWei.toString(), unwrappedWei: stats.unwrappedWei.toString(),
    balanceWei: balance.toString(), dryRun: DRY_RUN,
  }));
}

main().catch((e) => { console.error(e); process.exit(1); });
