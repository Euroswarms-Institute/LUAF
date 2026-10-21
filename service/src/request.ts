/** Validating what a caller may ask for.
 *
 *  The CLI took its settings from the operator's own environment, so there was
 *  nothing to validate: a flag you typed was a flag you meant. A request from
 *  the website is different, and the rule here is that a caller may choose
 *  *what to design*, never *where the money goes or how it is signed*.
 *
 *  So the allowed fields are the brief, the profile, the opening buy and the
 *  launch terms. Not the RPC, not the key, not the output directory, not the
 *  tenant, not the deployment. Those come from the server's own configuration
 *  and a request cannot reach them — which also means a path cannot arrive
 *  from outside and be written to. */

import { baseSpec, validateSpec, type LaunchSpec } from "@lucreocracy/core";

export interface RunRequest {
  profile?: string;
  topic?: string;
  firstBuyWei?: bigint;
  /** Asks for a real launch. Honoured only if the server may sign at all. */
  live: boolean;
  /** Design and validate, touch no chain. */
  designOnly: boolean;
  tax?: Partial<LaunchSpec["tax"]>;
  minHoldForDividends?: number;
  dividendMode?: number;
}

export type Parsed<T> = { ok: true; value: T } | { ok: false; error: string };

const MAX_TOPIC = 2_000;

function asBool(v: unknown, field: string): Parsed<boolean> {
  if (v === undefined || v === null) return { ok: true, value: false };
  if (typeof v !== "boolean") return { ok: false, error: `${field} must be a boolean` };
  return { ok: true, value: v };
}

/** Wei arrives as a decimal string: a wei amount does not survive JSON's
 *  number type, and a silently rounded opening buy is real money. */
function asWei(v: unknown, field: string): Parsed<bigint | undefined> {
  if (v === undefined || v === null) return { ok: true, value: undefined };
  if (typeof v === "number") {
    return { ok: false, error: `${field} must be a decimal string, not a number: JSON cannot hold a wei amount` };
  }
  if (typeof v !== "string" || !/^\d+$/.test(v)) {
    return { ok: false, error: `${field} must be a string of digits (wei)` };
  }
  return { ok: true, value: BigInt(v) };
}

function asInt(v: unknown, field: string, min: number, max: number): Parsed<number | undefined> {
  if (v === undefined || v === null) return { ok: true, value: undefined };
  if (typeof v !== "number" || !Number.isInteger(v) || v < min || v > max) {
    return { ok: false, error: `${field} must be an integer from ${min} to ${max}` };
  }
  return { ok: true, value: v };
}

export function parseRunRequest(body: string): Parsed<RunRequest> {
  let raw: unknown;
  try {
    raw = body.trim() ? JSON.parse(body) : {};
  } catch (e) {
    return { ok: false, error: `body is not JSON: ${(e as Error).message}` };
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return { ok: false, error: "body must be a JSON object" };
  }
  const o = raw as Record<string, unknown>;

  // Naming a field the server owns is a mistake worth reporting, not ignoring:
  // a caller who sends `rpcUrl` believes it took effect.
  const forbidden = ["rpcUrl", "privateKey", "outDir", "tenant", "chainId", "deployment", "dryRun", "mode", "runtime"];
  const offered = forbidden.filter((k) => k in o);
  if (offered.length) {
    return {
      ok: false,
      error: `${offered.join(", ")} ${offered.length === 1 ? "is" : "are"} set by the server, not the caller`,
    };
  }

  const live = asBool(o.live, "live");
  if (!live.ok) return live;
  const designOnly = asBool(o.designOnly, "designOnly");
  if (!designOnly.ok) return designOnly;
  const firstBuyWei = asWei(o.firstBuyWei, "firstBuyWei");
  if (!firstBuyWei.ok) return firstBuyWei;
  const minHold = asInt(o.minHoldForDividends, "minHoldForDividends", 0, 10_000_000);
  if (!minHold.ok) return minHold;
  const dividendMode = asInt(o.dividendMode, "dividendMode", 0, 1);
  if (!dividendMode.ok) return dividendMode;

  if (o.profile !== undefined && (typeof o.profile !== "string" || !/^[a-z0-9_]+$/.test(o.profile))) {
    return { ok: false, error: "profile must be a lowercase identifier" };
  }
  if (o.topic !== undefined && typeof o.topic !== "string") return { ok: false, error: "topic must be a string" };
  if (typeof o.topic === "string" && o.topic.length > MAX_TOPIC) {
    return { ok: false, error: `topic must be at most ${MAX_TOPIC} characters` };
  }

  let tax: Partial<LaunchSpec["tax"]> | undefined;
  if (o.tax !== undefined) {
    if (typeof o.tax !== "object" || o.tax === null || Array.isArray(o.tax)) {
      return { ok: false, error: "tax must be an object" };
    }
    const t = o.tax as Record<string, unknown>;
    tax = {};
    for (const k of ["buyTaxBps", "sellTaxBps", "devBps", "dividendBps", "liquidityBps", "mmBps"] as const) {
      if (!(k in t)) continue;
      const v = asInt(t[k], `tax.${k}`, 0, 10_000);
      if (!v.ok) return v;
      tax[k] = v.value;
    }
  }

  return {
    ok: true,
    value: {
      profile: o.profile as string | undefined,
      topic: o.topic as string | undefined,
      firstBuyWei: firstBuyWei.value,
      live: live.value,
      designOnly: designOnly.value,
      tax,
      minHoldForDividends: minHold.value,
      dividendMode: dividendMode.value,
    },
  };
}

/** Build the spec the pipeline runs, taking tenant and chain from the server's
 *  own deployment so a request can never redirect a launch. */
export function specFromRequest(
  request: RunRequest,
  server: { tenant: string; chainId: number; defaultProfile: string },
): Parsed<LaunchSpec> {
  const spec = baseSpec({
    profile: request.profile ?? server.defaultProfile,
    topic: request.topic ?? "",
    tenant: server.tenant,
    chainId: server.chainId,
    ...(request.firstBuyWei !== undefined ? { firstBuyWei: request.firstBuyWei } : {}),
    ...(request.minHoldForDividends !== undefined ? { minHoldForDividends: request.minHoldForDividends } : {}),
    ...(request.dividendMode !== undefined ? { dividendMode: request.dividendMode } : {}),
  });
  if (request.tax) spec.tax = { ...spec.tax, ...request.tax };
  try {
    validateSpec(spec);
  } catch (e) {
    return { ok: false, error: (e as Error).message };
  }
  return { ok: true, value: spec };
}
