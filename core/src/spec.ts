/** A launch spec is the pipeline's unit of work and the genome the selection
 *  loop breeds: everything that decides what gets launched and on what terms.
 *  Fee income is joined back to a spec through `specId`, which the Launcher
 *  writes into its `Launched` event, so a spec must hash to a stable id. */

import { createHash } from "node:crypto";

/** Which raise the coin opens with. Open is the free curve (no deadline, no
 *  founder cut of the raise, creator paid out of curve fees); Guaranteed is
 *  all-or-nothing with refunds. The Launcher only runs Open: a Guaranteed
 *  raise needs a founder who can be held to a target. */
export const RAISE_OPEN = 1;

/** How the founder tax splits once a coin graduates. The four shares are
 *  basis points and must sum to 10000; the factory rejects anything else. */
export interface TaxPolicy {
  /** Tax on buys, in bps, 0..400. */
  buyTaxBps: number;
  /** Tax on sells, in bps, 0..400. */
  sellTaxBps: number;
  /** Paid to the dev wallet, which the Launcher forces to the creator splitter. */
  devBps: number;
  /** Paid to holders as dividends in the pair token. */
  dividendBps: number;
  /** Added to the pool as permanent auto-liquidity. */
  liquidityBps: number;
  /** Placed as market-making quote walls. */
  mmBps: number;
}

/** The runtime the designer writes the agent in. The shipped profile prompts
 *  all specify Python, so that is the default; the field exists so a profile
 *  can ask for something else without the pipeline caring. */
export type Runtime = "python" | "node";

export interface LaunchSpec {
  /** Profile directory under `data/profiles`, e.g. "crypto". Chooses the
   *  designer's system prompt, topic prompt and product focus. */
  profile: string;
  /** The brief. Empty means the LLM generates one from the profile's topic prompt. */
  topic: string;
  /** Tenant slug; picks the `launcher-<slug>-<chain>.json` deployment. */
  tenant: string;
  /** Chain id the tenant stack is deployed on. */
  chainId: number;
  /** Always RAISE_OPEN for now; kept explicit so the genome records it. */
  mode: number;
  tax: TaxPolicy;
  /** Whole-token floor to earn dividends; 0 means every holder earns. */
  minHoldForDividends: number;
  /** 0 linear, 1 tiered (bigger holdings earn more per token). */
  dividendMode: number;
  /** Wei the operator puts into the first buy. Must fit the factory's
   *  launch-window cap (1% of the graduation target in the first 60 s) or the
   *  launch reverts, so the publisher clamps it. */
  firstBuyWei: bigint;
  runtime: Runtime;
  /** What this spec descends from, for lineage. Empty for a founder spec. */
  parents?: string[];
}

/** The format is what saturates: the shape of a launch with its one-off
 *  details (topic, tenant) removed. Fitness divides fee income by the number
 *  of live copies of a format, so two specs that differ only in their brief
 *  are the same format and compete with each other. */
export function formatKey(s: LaunchSpec): string {
  const t = s.tax;
  return [
    s.profile,
    s.chainId,
    s.mode,
    s.runtime,
    `${t.buyTaxBps}/${t.sellTaxBps}`,
    `${t.devBps}/${t.dividendBps}/${t.liquidityBps}/${t.mmBps}`,
    `hold${s.minHoldForDividends}`,
    `div${s.dividendMode}`,
  ].join(":");
}

/** The on-chain id: a hash over every field, so the event ties a coin to this
 *  exact spec and not merely to its format. */
export function specId(s: LaunchSpec): `0x${string}` {
  const h = createHash("sha256")
    .update(
      JSON.stringify({
        profile: s.profile,
        topic: s.topic,
        tenant: s.tenant,
        chainId: s.chainId,
        mode: s.mode,
        tax: s.tax,
        minHoldForDividends: s.minHoldForDividends,
        dividendMode: s.dividendMode,
        firstBuyWei: s.firstBuyWei.toString(),
        runtime: s.runtime,
      }),
    )
    .digest("hex");
  return `0x${h}`;
}

export class SpecError extends Error {}

/** Reject a spec the factory would reject anyway, with a reason the operator
 *  can act on instead of a bare revert. */
export function validateSpec(s: LaunchSpec): void {
  const t = s.tax;
  const sum = t.devBps + t.dividendBps + t.liquidityBps + t.mmBps;
  if (sum !== 10_000) throw new SpecError(`tax buckets must sum to 10000, got ${sum}`);
  for (const [k, v] of [["buyTaxBps", t.buyTaxBps], ["sellTaxBps", t.sellTaxBps]] as const) {
    if (v < 0 || v > 400) throw new SpecError(`${k} must be 0..400, got ${v}`);
  }
  if (s.mode !== RAISE_OPEN) throw new SpecError("only Open mode launches are supported");
  if (s.minHoldForDividends < 0 || s.minHoldForDividends > 10_000_000) {
    throw new SpecError("minHoldForDividends must be 0..10000000 whole tokens (1% of supply)");
  }
  if (t.dividendBps === 0 && (s.minHoldForDividends !== 0 || s.dividendMode !== 0)) {
    throw new SpecError("a dividend floor or ladder needs a non-zero dividendBps");
  }
  if (s.dividendMode === 1 && s.minHoldForDividends === 0) {
    throw new SpecError("a tiered dividend ladder needs a non-zero floor");
  }
  if (s.firstBuyWei < 0n) throw new SpecError("firstBuyWei cannot be negative");
  if (!s.profile.trim()) throw new SpecError("spec needs a profile");
  if (!s.tenant.trim()) throw new SpecError("spec needs a tenant");
}

/** A middle-of-the-road starting spec. The selection loop mutates away from
 *  it; it is not meant to be optimal, only valid and unremarkable. */
export function baseSpec(overrides: Partial<LaunchSpec> = {}): LaunchSpec {
  return {
    profile: "crypto",
    topic: "",
    tenant: "",
    chainId: 46630,
    mode: RAISE_OPEN,
    tax: { buyTaxBps: 200, sellTaxBps: 300, devBps: 4000, dividendBps: 3000, liquidityBps: 1500, mmBps: 1500 },
    minHoldForDividends: 0,
    dividendMode: 0,
    firstBuyWei: 0n,
    runtime: "python",
    ...overrides,
  };
}
