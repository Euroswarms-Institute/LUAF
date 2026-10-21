/** What each launch actually earned.
 *
 *  This is the feedback the Python pipeline never had: its only measure of
 *  success was the wallet's SOL balance, which says nothing about which brief
 *  or which terms produced the money. Because the Launcher writes a `specId`
 *  into its `Launched` event, every fee can be traced to the spec that caused
 *  it, which is what the selection loop breeds on.
 *
 *  A note on units, because getting this wrong would corrupt fitness: curve
 *  fees are always ETH. Post-graduation the hook takes its fee in the swap's
 *  *unspecified* currency, so some of it arrives as the coin itself. Coin-
 *  denominated fees are counted separately and deliberately kept out of the
 *  ETH total — they are only worth what the treasury keeper later sells them
 *  for, and booking them at face value would flatter a format that earned
 *  nothing spendable. */

import {
  createPublicClient,
  http,
  parseAbi,
  defineChain,
  type Address,
  type Hex,
  type PublicClient,
} from "viem";

import type { LauncherDeployment } from "./config.js";

export const incomeAbi = parseAbi([
  "event Launched(bytes32 indexed specId, address indexed token, address indexed factory, uint256 firstBuyWei, uint256 tokensOut)",
  "event FeeAccrued(address indexed token, address indexed recipient, uint256 amount)",
  "event Graduated(address indexed token, bytes32 poolId, uint256 raisedWei, uint256 founderCutWei, uint256 pairSeeded)",
  "event FeeTaken(bytes32 indexed id, address currency, bool isBuy, uint256 platform, uint256 founderTax, uint256 sniper)",
  "event BucketsSettled(bytes32 indexed id, uint256 dev, uint256 dividends, uint256 liquidity, uint256 wall)",
]);

export interface TokenIncome {
  token: Address;
  specId: Hex;
  graduated: boolean;
  /** Our share of curve fees, as the coin's creator. ETH. */
  curveCreatorWei: bigint;
  /** The tenant stack's platform share of curve fees. ETH. */
  curvePlatformWei: bigint;
  /** Hook fees taken in the pair token (WETH), so ETH-equivalent. */
  poolPlatformPairWei: bigint;
  poolDevPairWei: bigint;
  /** Hook fees taken in the coin itself. Not ETH; not added to any total. */
  poolPlatformCoin: bigint;
  poolDevCoin: bigint;
  /** Everything above that is denominated in ETH. */
  totalEthWei: bigint;
  /** Trades the hook has charged on, a crude liveness signal. */
  swaps: number;
}

export interface SpecIncome {
  specId: Hex;
  tokens: Address[];
  graduated: number;
  totalEthWei: bigint;
  creatorEthWei: bigint;
  platformEthWei: bigint;
  swaps: number;
}

export interface IncomeOptions {
  /** First block to scan. Defaults to the deployment's own start block. */
  fromBlock?: bigint;
  toBlock?: bigint;
  /** Blocks per getLogs page. Public RPCs cap the span. */
  chunk?: bigint;
}

function chainFor(dep: LauncherDeployment, rpcUrl: string) {
  return defineChain({
    id: dep.chainId,
    name: `chain-${dep.chainId}`,
    nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [rpcUrl] } },
  });
}

/** getLogs in pages, because every public endpoint limits the block span. */
async function pagedLogs<T>(
  client: PublicClient,
  args: { address: Address; event: (typeof incomeAbi)[number]; fromBlock: bigint; toBlock: bigint; chunk: bigint },
): Promise<T[]> {
  const out: T[] = [];
  for (let start = args.fromBlock; start <= args.toBlock; start += args.chunk) {
    const end = start + args.chunk - 1n > args.toBlock ? args.toBlock : start + args.chunk - 1n;
    const logs = await client.getLogs({
      address: args.address,
      event: args.event as never,
      fromBlock: start,
      toBlock: end,
    });
    out.push(...(logs as unknown as T[]));
  }
  return out;
}

function event(name: string) {
  const e = incomeAbi.find((a) => a.type === "event" && a.name === name);
  if (!e) throw new Error(`no ${name} event in the income ABI`);
  return e;
}

/** Fee income for every coin this Launcher has launched. */
export async function collectIncome(
  dep: LauncherDeployment,
  rpcUrl: string,
  opts: IncomeOptions = {},
): Promise<TokenIncome[]> {
  const chain = chainFor(dep, rpcUrl);
  const client = createPublicClient({ chain, transport: http(rpcUrl) }) as PublicClient;
  const net = await client.getChainId();
  if (net !== dep.chainId) throw new Error(`RPC serves chain ${net}, deployment is for ${dep.chainId}`);

  const fromBlock = opts.fromBlock ?? BigInt(dep.startBlock ?? 0);
  const toBlock = opts.toBlock ?? (await client.getBlockNumber());
  const chunk = opts.chunk ?? 50_000n;
  const page = { fromBlock, toBlock, chunk };

  type LaunchedLog = { args: { specId: Hex; token: Address; factory: Address } };
  type FeeAccruedLog = { args: { token: Address; recipient: Address; amount: bigint } };
  type GraduatedLog = { args: { token: Address; poolId: Hex } };
  type FeeTakenLog = { args: { id: Hex; currency: Address; platform: bigint; founderTax: bigint; sniper: bigint } };
  type BucketsLog = { args: { id: Hex; dev: bigint } };

  const launched = await pagedLogs<LaunchedLog>(client, { address: dep.contracts.launcher, event: event("Launched"), ...page });
  const byToken = new Map<string, TokenIncome>();
  for (const l of launched) {
    const token = l.args.token;
    byToken.set(token.toLowerCase(), {
      token,
      specId: l.args.specId,
      graduated: false,
      curveCreatorWei: 0n,
      curvePlatformWei: 0n,
      poolPlatformPairWei: 0n,
      poolDevPairWei: 0n,
      poolPlatformCoin: 0n,
      poolDevCoin: 0n,
      totalEthWei: 0n,
      swaps: 0,
    });
  }
  if (byToken.size === 0) return [];

  const launcher = dep.contracts.launcher.toLowerCase();
  const platform = dep.contracts.platformSplitter.toLowerCase();
  const weth = dep.contracts.weth.toLowerCase();

  const accrued = await pagedLogs<FeeAccruedLog>(client, { address: dep.contracts.factory, event: event("FeeAccrued"), ...page });
  for (const a of accrued) {
    const row = byToken.get(a.args.token.toLowerCase());
    if (!row) continue; // a coin someone else launched on the same factory
    const to = a.args.recipient.toLowerCase();
    if (to === launcher) row.curveCreatorWei += a.args.amount;
    else if (to === platform) row.curvePlatformWei += a.args.amount;
  }

  const graduated = await pagedLogs<GraduatedLog>(client, { address: dep.contracts.factory, event: event("Graduated"), ...page });
  const poolToToken = new Map<string, string>();
  for (const g of graduated) {
    const key = g.args.token.toLowerCase();
    const row = byToken.get(key);
    if (!row) continue;
    row.graduated = true;
    poolToToken.set(g.args.poolId.toLowerCase(), key);
  }

  if (poolToToken.size > 0) {
    const taken = await pagedLogs<FeeTakenLog>(client, { address: dep.contracts.hook, event: event("FeeTaken"), ...page });
    for (const t of taken) {
      const tokenKey = poolToToken.get(t.args.id.toLowerCase());
      if (!tokenKey) continue;
      const row = byToken.get(tokenKey);
      if (!row) continue;
      row.swaps++;
      const currency = t.args.currency.toLowerCase();
      // The sniper premium is placed as quote walls, not paid out, so it is
      // not income to anyone and is left out of every total.
      if (currency === weth) row.poolPlatformPairWei += t.args.platform;
      else if (currency === tokenKey) row.poolPlatformCoin += t.args.platform;
    }
    const buckets = await pagedLogs<BucketsLog>(client, { address: dep.contracts.hook, event: event("BucketsSettled"), ...page });
    // BucketsSettled does not carry the currency; it settles the founder tax
    // of the swap that produced it. Pair the two by their position in the same
    // pool's stream, which is exact: the hook emits one of each per swap that
    // charges a founder tax.
    const takenByPool = new Map<string, FeeTakenLog[]>();
    for (const t of taken) {
      const k = t.args.id.toLowerCase();
      const list = takenByPool.get(k) ?? [];
      list.push(t);
      takenByPool.set(k, list);
    }
    const seen = new Map<string, number>();
    for (const b of buckets) {
      const poolKey = b.args.id.toLowerCase();
      const tokenKey = poolToToken.get(poolKey);
      if (!tokenKey) continue;
      const row = byToken.get(tokenKey);
      if (!row) continue;
      const list = (takenByPool.get(poolKey) ?? []).filter((t) => t.args.founderTax > 0n);
      const i = seen.get(poolKey) ?? 0;
      seen.set(poolKey, i + 1);
      const currency = list[i]?.args.currency.toLowerCase();
      if (currency === weth) row.poolDevPairWei += b.args.dev;
      else if (currency === tokenKey) row.poolDevCoin += b.args.dev;
    }
  }

  for (const row of byToken.values()) {
    row.totalEthWei = row.curveCreatorWei + row.curvePlatformWei + row.poolPlatformPairWei + row.poolDevPairWei;
  }
  return [...byToken.values()];
}

/** Roll per-coin income up to the spec that produced it. */
export function bySpec(rows: TokenIncome[]): Map<string, SpecIncome> {
  const out = new Map<string, SpecIncome>();
  for (const r of rows) {
    const key = r.specId.toLowerCase();
    const agg = out.get(key) ?? {
      specId: r.specId,
      tokens: [],
      graduated: 0,
      totalEthWei: 0n,
      creatorEthWei: 0n,
      platformEthWei: 0n,
      swaps: 0,
    };
    agg.tokens.push(r.token);
    if (r.graduated) agg.graduated++;
    agg.totalEthWei += r.totalEthWei;
    agg.creatorEthWei += r.curveCreatorWei + r.poolDevPairWei;
    agg.platformEthWei += r.curvePlatformWei + r.poolPlatformPairWei;
    agg.swaps += r.swaps;
    out.set(key, agg);
  }
  return out;
}
