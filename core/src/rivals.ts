/** Rival counts: how crowded the market is for what we are about to launch.
 *
 *  Selection divides a format's income by the number of live copies of it. Our
 *  own copies it counts exactly. The rest of the market it cannot, so this
 *  reads the aggregator, which indexes every launchpad it is pointed at.
 *
 *  What comes back is per *chain*, not per format, and that is a real limit
 *  rather than a rounding: a third-party coin's terms are not in its launch
 *  event, so nobody can say which of our formats it is a copy of. The
 *  projection is therefore coarse in the safe direction — every coin launched
 *  on the chain in the window counts as a rival to every format on that chain.
 *  It overstates competition, so the loop backs off a crowded chain early
 *  instead of finding out by losing money there.
 *
 *  Without an aggregator the loop still works; it just sees only our own
 *  copies, which is the right behaviour for a chain where we are the only
 *  launchpad and too optimistic anywhere else. */

import { formatKey, type LaunchSpec } from "./spec.js";

export interface SaturationResponse {
  windowDays: number;
  since: number;
  rivalsByChain: Record<string, number>;
  chains?: { chainId: number; launches: number; graduations: number; graduationRate: number | null }[];
}

export class RivalsError extends Error {}

/** Fetch the aggregator's saturation report. */
export async function fetchSaturation(baseUrl: string, windowDays?: number): Promise<SaturationResponse> {
  const url = new URL("/api/saturation", baseUrl.replace(/\/+$/, "") + "/");
  if (windowDays) url.searchParams.set("windowDays", String(windowDays));
  let res: Response;
  try {
    res = await fetch(url, { signal: AbortSignal.timeout(20_000) });
  } catch (e) {
    throw new RivalsError(`aggregator unreachable at ${url}: ${(e as Error).message}`);
  }
  if (!res.ok) throw new RivalsError(`aggregator returned HTTP ${res.status}`);
  const json = (await res.json()) as SaturationResponse;
  if (!json || typeof json.rivalsByChain !== "object" || json.rivalsByChain === null) {
    throw new RivalsError("aggregator response has no rivalsByChain");
  }
  return json;
}

/** Turn a per-chain count into the per-format map selection expects.
 *
 *  Our own coins are already counted by the population, so they are subtracted
 *  from the chain's total before it is spread across that chain's formats —
 *  otherwise every one of our launches would be counted twice, once as a copy
 *  and once as a rival, and the loop would see twice the saturation it faces. */
export function rivalsForSpecs(
  report: SaturationResponse,
  specs: { spec: LaunchSpec; copies: number }[],
): Record<string, number> {
  const ourCopiesByChain = new Map<number, number>();
  for (const { spec, copies } of specs) {
    ourCopiesByChain.set(spec.chainId, (ourCopiesByChain.get(spec.chainId) ?? 0) + copies);
  }
  const out: Record<string, number> = {};
  for (const { spec } of specs) {
    const total = report.rivalsByChain[String(spec.chainId)] ?? 0;
    const ours = ourCopiesByChain.get(spec.chainId) ?? 0;
    out[formatKey(spec)] = Math.max(0, total - ours);
  }
  return out;
}

/** Read the aggregator and build the rivals map, or give up quietly.
 *
 *  A missing aggregator must not stop a launch: the loop falls back to counting
 *  only our own copies. The caller is told which happened so it can say so
 *  rather than silently reporting optimistic fitness. */
export async function loadRivals(
  baseUrl: string | undefined,
  specs: { spec: LaunchSpec; copies: number }[],
  windowDays?: number,
): Promise<{ rivals: Record<string, number>; source: "aggregator" | "none"; note?: string }> {
  if (!baseUrl) return { rivals: {}, source: "none", note: "no aggregator configured; counting only our own copies" };
  try {
    const report = await fetchSaturation(baseUrl, windowDays);
    return { rivals: rivalsForSpecs(report, specs), source: "aggregator" };
  } catch (e) {
    return { rivals: {}, source: "none", note: (e as Error).message };
  }
}
