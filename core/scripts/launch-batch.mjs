// Launch a batch of real coins across varied launch formats, take each one to
// graduation and trade it, so the selection loop has fee income to divide
// instead of a number that does not exist.
//
//   LUCRE_DRY_RUN=0 LUCRE_PRIVATE_KEY=0x… LUCRE_RPC_URL=… \
//   LAUNCHER_DEPLOYMENT=../contracts/deployments/launcher-lucre-46630.json \
//   ETH_USD_8=1000000000000000 \
//   node scripts/launch-batch.mjs [specs-per-format] [copies-per-spec]
//
// It goes through core's own publish(), not a hand-rolled launch, so what this
// measures is the production path. The LLM half is skipped: a Product is
// supplied directly, because the fee income of a launch format does not depend
// on what the designer wrote in the description, and an LLM call per coin would
// dominate the cost of the experiment.
//
// What this does and does not measure. Every coin is traded on the same
// schedule, so the differences between formats here are differences in fee
// *capture* -- how the hook's toll and the founder tax split under each
// parameter set -- and not differences in demand. Real demand is what the
// aggregator's copy counts are for, and it cannot be synthesised. Treat the
// output as a measured fee-mechanics table, which is what the breeder was
// missing, not as evidence that one format sells better than another.
import { readFileSync, writeFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import { createPublicClient, createWalletClient, http, parseAbi } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { baseSpec, specId, formatKey, publish, chainFor, collectIncome, bySpec } from "../dist/index.js";

const DEP_PATH = process.env.LAUNCHER_DEPLOYMENT;
if (!DEP_PATH) throw new Error("set LAUNCHER_DEPLOYMENT");
const dep = JSON.parse(readFileSync(isAbsolute(DEP_PATH) ? DEP_PATH : resolve(process.cwd(), DEP_PATH), "utf8"));
const ETH_USD_8 = BigInt(process.env.ETH_USD_8 ?? 1_000_000_000_000_000n);
const COPIES = Number(process.argv[3] ?? 2);

const run = {
  dryRun: process.env.LUCRE_DRY_RUN === "0" ? false : true,
  rpcUrl: process.env.LUCRE_RPC_URL ?? "https://rpc.testnet.chain.robinhood.com",
  privateKey: process.env.LUCRE_PRIVATE_KEY,
  maxSteps: 1,
  minLines: 1,
  validationTimeoutMs: 1,
  outDir: process.env.LUCRE_OUT_DIR ?? "/tmp",
};

// The formats under test. Each varies one thing that plausibly changes what a
// coin earns, so a difference in the table can be attributed to something.
//
//   tax      the founder tax the hook charges on post-graduation trades, and
//            how it is split between the dev bucket, dividends, liquidity and
//            the market-maker wall
//   dividendMode  0 linear, 1 tiered: bigger holdings earn more per token
//   minHoldForDividends  a whole-token floor to earn anything
const FORMATS = [
  { label: "baseline", tax: { buyTaxBps: 200, sellTaxBps: 300, devBps: 4000, dividendBps: 3000, liquidityBps: 1500, mmBps: 1500 }, dividendMode: 0, minHoldForDividends: 0 },
  // 0/0 is not a format that can be tested: the hook's sniper-premium path
  // leaves a V4 delta unsettled when there is no founder tax, so the coin
  // reverts every trade for its first fifteen seconds. validateSpec refuses it.
  // Taxing one side only is the nearest thing that works, and a more
  // interesting question anyway -- whether charging exits alone earns more.
  { label: "exit-tax-only", tax: { buyTaxBps: 0, sellTaxBps: 400, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500 }, dividendMode: 0, minHoldForDividends: 0 },
  { label: "max-tax", tax: { buyTaxBps: 400, sellTaxBps: 400, devBps: 4000, dividendBps: 3000, liquidityBps: 1500, mmBps: 1500 }, dividendMode: 0, minHoldForDividends: 0 },
  { label: "dev-heavy", tax: { buyTaxBps: 300, sellTaxBps: 300, devBps: 7000, dividendBps: 1000, liquidityBps: 1000, mmBps: 1000 }, dividendMode: 0, minHoldForDividends: 0 },
  { label: "liquidity-heavy", tax: { buyTaxBps: 300, sellTaxBps: 300, devBps: 1000, dividendBps: 1000, liquidityBps: 7000, mmBps: 1000 }, dividendMode: 0, minHoldForDividends: 0 },
  { label: "tiered-dividends", tax: { buyTaxBps: 300, sellTaxBps: 300, devBps: 2500, dividendBps: 4500, liquidityBps: 1500, mmBps: 1500 }, dividendMode: 1, minHoldForDividends: 1_000_000 },
];

const product = (label, i) => ({
  name: `Lucre ${label} ${i}`,
  ticker: `L${label.replace(/[^a-zA-Z]/g, "").slice(0, 4).toUpperCase()}${i}`,
  description: `Selection-loop probe for the ${label} launch format.`,
  code: "# probe\n",
  language: "python",
  useCases: [],
  requirements: [],
  tags: `probe,${label}`,
  raw: {},
});

const factoryAbi = parseAbi([
  "function graduationRaiseWei() view returns (uint256)",
  "function buy(address coin, uint256 minTokensOut) payable returns (uint256)",
  "function curveState(address) view returns (uint64 deadline, uint256 price, uint256 soldWhole, uint256 remainingWhole, uint256 raisedWei, uint256 targetRaiseWei, bool finalized, bool aborted)",
]);
const routerAbi = parseAbi(["function buy(address coin, bytes v3Path, uint256 minCoinOut) payable returns (uint256)"]);

// A dry run has no key, and reads do not need one.
const account = run.dryRun ? undefined : privateKeyToAccount(run.privateKey);
const chain = chainFor(dep.chainId, run.rpcUrl);
const pub = createPublicClient({ chain, transport: http(run.rpcUrl) });
const wallet = account ? createWalletClient({ account, chain, transport: http(run.rpcUrl) }) : undefined;

const target = await pub.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "graduationRaiseWei" });
const SWAP = target / 20n;
const SWAPS = Number(process.env.SWAPS ?? 3);

async function send(label, request) {
  const hash = await wallet.writeContract(request);
  const rc = await pub.waitForTransactionReceipt({ hash });
  if (rc.status !== "success") throw new Error(`${label} reverted: ${hash}`);
  return rc;
}

/** Buy until the curve graduates. The curve refunds what it cannot take, so
 *  overshooting the last buy costs only gas. */
async function graduate(coin) {
  for (let i = 0; i < 40; i++) {
    const st = await pub.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "curveState", args: [coin] });
    if (st[6]) return true;
    const left = target - st[4];
    const step = left > target / 4n ? target / 4n : left + target / 1000n;
    await send(`buy ${coin}`, {
      address: dep.contracts.factory, abi: factoryAbi, functionName: "buy", args: [coin, 0n], value: step,
    });
  }
  return false;
}

/** Trade the graduated pool. Every router trade carries headroom over the
 *  estimate: the swap runs inside PoolManager.unlock, and EIP-150 gives that
 *  nested call at most 63/64 of the gas remaining, so a transaction sent at
 *  exactly its estimate runs out of gas while a larger limit succeeds using
 *  less. See DEPLOY.md. */
async function trade(coin) {
  let done = 0;
  for (let i = 0; i < SWAPS; i++) {
    const args = { address: dep.contracts.router, abi: routerAbi, functionName: "buy", args: [coin, "0x", 0n], value: SWAP, account };
    const est = await pub.estimateContractGas(args);
    await send(`swap ${coin}`, { ...args, gas: (est * 125n) / 100n });
    done++;
  }
  return done;
}

// Launch everything first, then graduate everything. The factory caps a wallet
// at target/100 for the first 60 seconds after a launch, and the buys that
// graduate a curve are far larger than that, so each coin has to clear its
// window before it can be bought up. Launching the batch and waiting once
// spends that minute for all of them instead of once per coin.
const launched = [];
for (const fmt of FORMATS) {
  for (let copy = 0; copy < COPIES; copy++) {
    const spec = baseSpec({
      tenant: dep.tenant,
      chainId: dep.chainId,
      topic: `${fmt.label}-${copy}`,
      tax: fmt.tax,
      dividendMode: fmt.dividendMode,
      minHoldForDividends: fmt.minHoldForDividends,
      firstBuyWei: target / 100n,
    });
    const id = specId(spec);
    process.stdout.write(`${fmt.label} copy ${copy}  spec ${id.slice(0, 10)}  `);
    const res = await publish({ spec, product: product(fmt.label, copy), dep, run, ethUsd8: ETH_USD_8 });
    if (res.dryRun) { console.log("dry run"); continue; }
    console.log(res.token);
    launched.push({ format: fmt.label, formatKey: formatKey(spec), specId: id, token: res.token, graduated: false, swaps: 0 });
  }
}

if (launched.length > 0) {
  console.log(`\nwaiting out the 60s launch window for ${launched.length} coin(s)...`);
  await new Promise((done) => setTimeout(done, 65_000));
  for (const l of launched) {
    l.graduated = await graduate(l.token);
    // The hook charges a sniper premium for 15 seconds after graduation. It is
    // a different fee regime from the one a coin spends its life in, so trading
    // inside it would measure something else, and on a coin with no founder tax
    // it reverts the trade outright.
    if (l.graduated) await new Promise((r) => setTimeout(r, 16_000));
    l.swaps = l.graduated ? await trade(l.token) : 0;
    console.log(`${l.format.padEnd(18)} ${l.token}  graduated ${l.graduated}  swaps ${l.swaps}`);
  }
}

if (launched.length === 0) { console.log("\nnothing launched (dry run)"); process.exit(0); }

// The income table, read back off the chain by the same collector the
// persistent loop uses.
const rows = await collectIncome({ dep, rpcUrl: run.rpcUrl }, { chunk: 10_000n });
const specs = bySpec(rows);
const byFormat = new Map();
for (const l of launched) {
  const inc = specs.get(l.specId);
  if (!inc) continue;
  const agg = byFormat.get(l.format) ?? { format: l.format, formatKey: l.formatKey, coins: 0, totalEthWei: 0n, creatorEthWei: 0n, platformEthWei: 0n, swaps: 0, turnoverSecs: 0 };
  agg.coins += inc.tokens.length;
  agg.totalEthWei += inc.totalEthWei;
  agg.creatorEthWei += inc.creatorEthWei;
  agg.platformEthWei += inc.platformEthWei;
  agg.swaps += inc.swaps;
  agg.turnoverSecs += inc.meanTurnoverSecs;
  byFormat.set(l.format, agg);
}

console.log(`\n${"format".padEnd(18)} ${"coins".padStart(5)} ${"total ETH".padStart(22)} ${"creator".padStart(22)} ${"platform".padStart(22)} ${"swaps".padStart(5)} turnover`);
for (const a of byFormat.values()) {
  const eth = (w) => (Number(w) / 1e18).toFixed(18);
  console.log(`${a.format.padEnd(18)} ${String(a.coins).padStart(5)} ${eth(a.totalEthWei).padStart(22)} ${eth(a.creatorEthWei).padStart(22)} ${eth(a.platformEthWei).padStart(22)} ${String(a.swaps).padStart(5)} ${Math.round(a.turnoverSecs / Math.max(1, a.coins))}s`);
}

const out = process.env.INCOME_OUT ?? "income.json";
writeFileSync(out, JSON.stringify({
  chainId: dep.chainId, tenant: dep.tenant, launcher: dep.contracts.launcher, at: Math.floor(Date.now() / 1000),
  launched,
  formats: [...byFormat.values()].map((a) => ({ ...a, totalEthWei: a.totalEthWei.toString(), creatorEthWei: a.creatorEthWei.toString(), platformEthWei: a.platformEthWei.toString() })),
  specs: [...specs.values()].map((s) => ({ ...s, totalEthWei: s.totalEthWei.toString(), creatorEthWei: s.creatorEthWei.toString(), platformEthWei: s.platformEthWei.toString() })),
}, null, 2));
console.log(`\nwrote ${out}`);
