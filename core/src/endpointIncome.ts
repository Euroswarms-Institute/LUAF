/** Endpoint revenue: the only income here that is not a transfer between
 *  traders.
 *
 *  Everything else this business books is a fee on circulation. Capital I
 *  ch. 5 is blunt about what that is worth in aggregate: "If equivalents are
 *  exchanged, no surplus-value results, and if non-equivalents are exchanged,
 *  still no surplus-value. Circulation, or the exchange of commodities, begets
 *  no value." Capital III ch. 17 says the same of the middleman: merchant
 *  capital "creates neither value nor surplus-value, but acts as middleman in
 *  their realisation". Curve fees, pool tolls and router fees are all shares
 *  of value produced somewhere else, and in a closed market they are shares of
 *  what the traders lose.
 *
 *  Someone paying an agent to do a job is different in kind. That is outside
 *  money entering, and it is the only line that could still be there when the
 *  speculative inflow stops. `income.ts` measures the fee side exhaustively
 *  and measured this not at all, which meant the business could not tell
 *  whether it had any real income.
 *
 *  **Unique payers matters more than the total.** Capital I ch. 5 again: "The
 *  capitalist class, as a whole, in any country, cannot over-reach
 *  themselves." Revenue arriving from two addresses over and over is us paying
 *  ourselves, or one customer, and tells us almost nothing; the same amount
 *  from fifty is a market. Both are reported, and the total is never given
 *  without the payer count beside it. */

import { createPublicClient, defineChain, http, parseAbi, type Address, type PublicClient } from "viem";

export const erc20TransferAbi = parseAbi([
  "event Transfer(address indexed from, address indexed to, uint256 value)",
]);

export interface EndpointIncome {
  /** The recipient these payments were addressed to. */
  payTo: Address;
  asset: Address;
  chainId: number;
  /** Total received, in the asset's smallest unit. */
  totalAtomic: bigint;
  /** How many payments arrived. */
  payments: number;
  /** Distinct paying addresses. The number that says whether this is a market
   *  or an echo. */
  uniquePayers: number;
  /** Received from the single largest payer, in atomic units. A total that is
   *  nearly all one payer is one customer, not demand. */
  largestPayerAtomic: bigint;
  /** Blocks scanned, so a caller can resume. */
  fromBlock: bigint;
  toBlock: bigint;
}

export interface EndpointIncomeRequest {
  /** RPC for the *payment* chain, which is not the launch chain. */
  rpcUrl: string;
  chainId: number;
  /** The ERC-20 payments settle in, e.g. USDC. */
  asset: Address;
  payTo: Address;
  fromBlock?: bigint;
  toBlock?: bigint;
  /** Blocks per getLogs page. */
  chunk?: bigint;
}

export class EndpointIncomeError extends Error {}

/** Read what the endpoints have actually been paid.
 *
 *  Payments are read from the asset's own Transfer log rather than from any
 *  facilitator's API: a settled transfer is the fact, and a facilitator's
 *  record of one is a claim about it. */
export async function collectEndpointIncome(req: EndpointIncomeRequest): Promise<EndpointIncome> {
  const chain = defineChain({
    id: req.chainId,
    name: `chain-${req.chainId}`,
    nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [req.rpcUrl] } },
  });
  const client = createPublicClient({ chain, transport: http(req.rpcUrl) }) as PublicClient;
  const served = await client.getChainId();
  if (served !== req.chainId) {
    throw new EndpointIncomeError(`RPC serves chain ${served}, payments settle on ${req.chainId}`);
  }

  const toBlock = req.toBlock ?? (await client.getBlockNumber());
  const fromBlock = req.fromBlock ?? 0n;
  const chunk = req.chunk ?? 50_000n;

  const byPayer = new Map<string, bigint>();
  let totalAtomic = 0n;
  let payments = 0;

  for (let start = fromBlock; start <= toBlock; start += chunk) {
    const end = start + chunk - 1n > toBlock ? toBlock : start + chunk - 1n;
    const logs = await client.getLogs({
      address: req.asset,
      event: erc20TransferAbi[0],
      // Filtered on the indexed recipient, so the node does the work rather
      // than shipping every transfer of the asset over the wire.
      args: { to: req.payTo },
      fromBlock: start,
      toBlock: end,
    });
    for (const l of logs) {
      const args = (l as unknown as { args?: { from?: Address; value?: bigint } }).args;
      const value = args?.value ?? 0n;
      const from = args?.from?.toLowerCase();
      if (!from || value === 0n) continue;
      totalAtomic += value;
      payments++;
      byPayer.set(from, (byPayer.get(from) ?? 0n) + value);
    }
  }

  let largestPayerAtomic = 0n;
  for (const v of byPayer.values()) if (v > largestPayerAtomic) largestPayerAtomic = v;

  return {
    payTo: req.payTo,
    asset: req.asset,
    chainId: req.chainId,
    totalAtomic,
    payments,
    uniquePayers: byPayer.size,
    largestPayerAtomic,
    fromBlock,
    toBlock,
  };
}

export interface ConcentrationVerdict {
  /** Share of revenue from the largest single payer, 0..1, or null with none. */
  topPayerShare: number | null;
  /** Whether this reads as a market rather than one customer or an echo. */
  isMarket: boolean;
  note: string;
}

/**
 * Is this revenue a market, or is it us paying ourselves?
 *
 * Not a moral question — a practical one. Revenue concentrated in one or two
 * addresses does not survive those addresses losing interest, and it is
 * indistinguishable from wash payments made to make an agent look earning.
 * The research on agent reputation found exactly this failure elsewhere: most
 * reviewers on the largest chains showed coordinated behaviour, and registry
 * counts overstated real usage by an order of magnitude.
 */
export function concentration(
  income: EndpointIncome,
  opts: { minPayers?: number; maxTopShare?: number } = {},
): ConcentrationVerdict {
  const minPayers = opts.minPayers ?? 5;
  const maxTopShare = opts.maxTopShare ?? 0.6;
  if (income.totalAtomic === 0n) {
    return { topPayerShare: null, isMarket: false, note: "no endpoint revenue yet" };
  }
  const topPayerShare = Number(income.largestPayerAtomic) / Number(income.totalAtomic);
  if (income.uniquePayers < minPayers) {
    return {
      topPayerShare,
      isMarket: false,
      note: `${income.uniquePayers} payer(s): too few to call this demand (want ${minPayers})`,
    };
  }
  if (topPayerShare > maxTopShare) {
    return {
      topPayerShare,
      isMarket: false,
      note: `${(topPayerShare * 100).toFixed(0)}% of revenue is one payer: a customer, not a market`,
    };
  }
  return {
    topPayerShare,
    isMarket: true,
    note: `${income.uniquePayers} payers, largest ${(topPayerShare * 100).toFixed(0)}%`,
  };
}

/** Format an atomic amount for a human, given the asset's decimals. */
export function formatAtomic(atomic: bigint, decimals = 6): string {
  const base = 10n ** BigInt(decimals);
  const whole = atomic / base;
  const frac = (atomic % base).toString().padStart(decimals, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole.toString();
}
