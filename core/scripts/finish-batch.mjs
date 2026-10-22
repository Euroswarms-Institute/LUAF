// Take every coin a Launcher has opened to graduation and trade it, skipping
// the ones already done, then print the income table.
//
//   LUCRE_PRIVATE_KEY=0x… LUCRE_RPC_URL=… \
//   LAUNCHER_DEPLOYMENT=../contracts/deployments/launcher-lucre-46630.json \
//   node scripts/finish-batch.mjs
//
// Separate from launch-batch.mjs because a batch that dies partway has already
// paid for its launches: the coins exist, and the only way to recover that
// spend is to finish them. It is also how a batch is re-run after the income
// table is wanted again.
import { readFileSync, writeFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import { createPublicClient, createWalletClient, http, parseAbi } from "viem";
import { privateKeyToAccount } from "viem/accounts";

import { chainFor, collectIncome, bySpec } from "../dist/index.js";

const DEP_PATH = process.env.LAUNCHER_DEPLOYMENT;
if (!DEP_PATH) throw new Error("set LAUNCHER_DEPLOYMENT");
const dep = JSON.parse(readFileSync(isAbsolute(DEP_PATH) ? DEP_PATH : resolve(process.cwd(), DEP_PATH), "utf8"));
const rpcUrl = process.env.LUCRE_RPC_URL ?? "https://rpc.testnet.chain.robinhood.com";
const SWAPS = Number(process.env.SWAPS ?? 3);

const factoryAbi = parseAbi([
  "function graduationRaiseWei() view returns (uint256)",
  "function totalTokens() view returns (uint256)",
  "function allTokens(uint256) view returns (address)",
  "function buy(address coin, uint256 minTokensOut) payable returns (uint256)",
  "function curveState(address) view returns (uint64 deadline, uint256 price, uint256 soldWhole, uint256 remainingWhole, uint256 raisedWei, uint256 targetRaiseWei, bool finalized, bool aborted)",
  "function listings(address) view returns (address creator, address pair, uint16 taxBps, bytes32 poolId, uint64 createdAt)",
]);
const routerAbi = parseAbi(["function buy(address coin, bytes v3Path, uint256 minCoinOut) payable returns (uint256)"]);

const account = privateKeyToAccount(process.env.LUCRE_PRIVATE_KEY);
const chain = chainFor(dep.chainId, rpcUrl);
const pub = createPublicClient({ chain, transport: http(rpcUrl) });
const wallet = createWalletClient({ account, chain, transport: http(rpcUrl) });
const factory = { address: dep.contracts.factory, abi: factoryAbi };

const target = await pub.readContract({ ...factory, functionName: "graduationRaiseWei" });
const SWAP = target / 20n;

async function send(label, request) {
  const hash = await wallet.writeContract(request);
  const rc = await pub.waitForTransactionReceipt({ hash });
  if (rc.status !== "success") throw new Error(`${label} reverted: ${hash}`);
  return rc;
}

async function graduate(coin) {
  for (let i = 0; i < 40; i++) {
    const st = await pub.readContract({ ...factory, functionName: "curveState", args: [coin] });
    if (st[6]) return true;
    const left = target - st[4];
    const step = left > target / 4n ? target / 4n : left + target / 1000n;
    await send(`buy ${coin}`, { ...factory, functionName: "buy", args: [coin, 0n], value: step });
  }
  return false;
}

/** Trade the graduated pool, with gas headroom over the estimate because the
 *  swap runs inside PoolManager.unlock and EIP-150 starves a nested call given
 *  exactly its estimate. See DEPLOY.md. */
async function trade(coin) {
  let done = 0;
  for (let i = 0; i < SWAPS; i++) {
    const args = { address: dep.contracts.router, abi: routerAbi, functionName: "buy", args: [coin, "0x", 0n], value: SWAP, account };
    try {
      const est = await pub.estimateContractGas(args);
      await send(`swap ${coin}`, { ...args, gas: (est * 125n) / 100n });
      done++;
    } catch (e) {
      console.log(`    swap ${i + 1} failed: ${String(e.shortMessage ?? e.message).split("\n")[0]}`);
      break;
    }
  }
  return done;
}

const n = Number(await pub.readContract({ ...factory, functionName: "totalTokens" }));
console.log(`${n} coin(s) on factory ${dep.contracts.factory}`);

const pending = [];
for (let i = 0; i < n; i++) {
  const coin = await pub.readContract({ ...factory, functionName: "allTokens", args: [BigInt(i)] });
  const st = await pub.readContract({ ...factory, functionName: "curveState", args: [coin] });
  pending.push({ coin, finalized: st[6], raisedWei: st[4] });
}
const todo = pending.filter((p) => !p.finalized);
console.log(`${pending.length - todo.length} already graduated, ${todo.length} to finish`);

for (const p of todo) {
  process.stdout.write(`  ${p.coin}  `);
  const grad = await graduate(p.coin);
  process.stdout.write(`graduated ${grad}`);
  if (!grad) { console.log(""); continue; }
  // The hook charges a 15% sniper premium for 5 seconds after graduation and
  // 5% for ten more. On a coin whose founder tax is zero that premium is the
  // only founder-side fee, which sends the hook down a path that leaves a V4
  // delta unsettled and reverts the trade with CurrencyNotSettled. Waiting the
  // window out costs 16 seconds and sidesteps it; the coins that carry a
  // founder tax are unaffected either way.
  await new Promise((r) => setTimeout(r, 16_000));
  const swaps = await trade(p.coin);
  console.log(`  swaps ${swaps}`);
}

const rows = await collectIncome({ dep, rpcUrl }, { chunk: 10_000n });
const specs = [...bySpec(rows).values()];
const eth = (w) => (Number(w) / 1e18).toFixed(18);
console.log(`\n${"specId".padEnd(12)} ${"coins".padStart(5)} ${"grad".padStart(4)} ${"total ETH".padStart(21)} ${"creator".padStart(21)} ${"platform".padStart(21)} ${"swaps".padStart(5)} turnover`);
for (const s of specs.sort((a, b) => (b.totalEthWei > a.totalEthWei ? 1 : -1))) {
  console.log(`${s.specId.slice(0, 10).padEnd(12)} ${String(s.tokens.length).padStart(5)} ${String(s.graduated).padStart(4)} ${eth(s.totalEthWei).padStart(21)} ${eth(s.creatorEthWei).padStart(21)} ${eth(s.platformEthWei).padStart(21)} ${String(s.swaps).padStart(5)} ${Math.round(s.meanTurnoverSecs)}s`);
}

const out = process.env.INCOME_OUT ?? "income.json";
writeFileSync(out, JSON.stringify({
  chainId: dep.chainId, tenant: dep.tenant, launcher: dep.contracts.launcher, at: Math.floor(Date.now() / 1000),
  specs: specs.map((s) => ({ ...s, totalEthWei: s.totalEthWei.toString(), creatorEthWei: s.creatorEthWei.toString(), platformEthWei: s.platformEthWei.toString() })),
}, null, 2));
console.log(`\nwrote ${out}`);
