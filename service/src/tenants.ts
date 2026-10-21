/** Who the service works for, and how far their authority reaches.
 *
 *  One process, many tenant stacks. The rule the single-tenant service was
 *  built on still holds — a request may never name its tenant — so the
 *  credential names it instead. A token resolves to exactly one grant, and the
 *  grant carries the deployment, the chain and the limits.
 *
 *  The limits are the part that matters, because one operator key signs for
 *  every tenant. A scope without a budget is not a narrowing: a tenant who can
 *  ask for launches without bound can spend our ETH without bound. So a grant
 *  carries a launch count and a spend ceiling, both debited *before* anything
 *  is signed and refunded only once we know nothing was. The conservative
 *  direction is deliberate: a crash between the debit and the launch leaves the
 *  tenant charged for a launch that may not have happened, which costs them a
 *  slot, where the other way round costs us ETH.
 *
 *  A grant can only narrow what the server itself may do. `maySign: true` on a
 *  dry-run server is still a dry run; nothing a grant says can widen that. */

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";

import { loadLauncherDeployment, type LauncherDeployment, type RunConfig } from "@lucreocracy/core";

export interface TenantGrant {
  slug: string;
  chainId: number;
  /** An explicit launcher deployment file. Otherwise found by slug and chain. */
  deployment?: string;
  /** Narrows the server's RPC, for a tenant on a different chain. */
  rpcUrl?: string;
  /** Whether this tenant's requests may sign at all, given a server that can. */
  maySign: boolean;
  /** Launches this grant may ever make. Undefined is unlimited. */
  maxLaunches?: number;
  /** Total wei of opening buys this grant may ever spend. Undefined is
   *  unlimited, which is only right for a grant held by the operator. */
  maxSpendWei?: bigint;
  /** Where the debits are kept. Defaults to <outDir>/<slug>/budget.json. */
  ledger?: string;
}

/** The token is kept only as a digest: nothing needs the token itself again,
 *  and a digest is not a credential. Lookup is by digest, which leaks nothing
 *  through timing — the only thing a probe's timing could describe is the
 *  digest the caller computed themselves, and sha256 does not run backwards. */
export function tokenDigest(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

export class GrantError extends Error {}

const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
const MIN_TOKEN = 24;

function asWei(v: unknown, where: string): bigint | undefined {
  if (v === undefined || v === null) return undefined;
  if (typeof v === "number") throw new GrantError(`${where}: give a wei amount as a decimal string, not a number`);
  if (typeof v !== "string" || !/^\d+$/.test(v)) throw new GrantError(`${where}: expected a string of digits (wei)`);
  return BigInt(v);
}

function asCount(v: unknown, where: string): number | undefined {
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "number" || !Number.isInteger(v) || v < 0) throw new GrantError(`${where}: expected a count`);
  return v;
}

/** Parse the grant file. Every problem is fatal: a service that starts with
 *  half its tenants loaded would answer one tenant's token with a 401 and give
 *  no reason anybody could act on. */
export function parseGrants(text: string): { grant: TenantGrant; token: string }[] {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (e) {
    throw new GrantError(`the grant file is not JSON: ${(e as Error).message}`);
  }
  const list = Array.isArray(raw) ? raw : (raw as { tenants?: unknown })?.tenants;
  if (!Array.isArray(list)) throw new GrantError("the grant file must be an array of grants, or { tenants: [...] }");

  const out: { grant: TenantGrant; token: string }[] = [];
  const slugs = new Set<string>();
  const digests = new Set<string>();

  for (const [i, entry] of list.entries()) {
    const where = `grant ${i}`;
    if (typeof entry !== "object" || entry === null) throw new GrantError(`${where}: expected an object`);
    const o = entry as Record<string, unknown>;

    const slug = typeof o.slug === "string" ? o.slug.trim() : "";
    if (!SLUG.test(slug)) throw new GrantError(`${where}: slug must be a lowercase name like "acme"`);
    if (slugs.has(slug)) throw new GrantError(`${where}: ${slug} appears twice`);
    slugs.add(slug);

    const token = typeof o.token === "string" ? o.token.trim() : "";
    if (token.length < MIN_TOKEN) throw new GrantError(`${slug}: token must be at least ${MIN_TOKEN} characters`);
    const digest = tokenDigest(token);
    // Two tenants sharing a token is a cross-tenant hole, not a convenience.
    if (digests.has(digest)) throw new GrantError(`${slug}: that token is already used by another grant`);
    digests.add(digest);

    const chainId = asCount(o.chainId, `${slug}.chainId`);
    if (!chainId) throw new GrantError(`${slug}: set chainId`);

    out.push({
      token,
      grant: {
        slug,
        chainId,
        deployment: typeof o.deployment === "string" ? o.deployment : undefined,
        rpcUrl: typeof o.rpcUrl === "string" ? o.rpcUrl : undefined,
        // Signing is off unless the grant says otherwise, so a grant written
        // without thinking about it cannot sign.
        maySign: o.maySign === true,
        maxLaunches: asCount(o.maxLaunches, `${slug}.maxLaunches`),
        maxSpendWei: asWei(o.maxSpendWei, `${slug}.maxSpendWei`),
        ledger: typeof o.ledger === "string" ? o.ledger : undefined,
      },
    });
  }
  if (!out.length) throw new GrantError("the grant file lists no tenants");
  return out;
}

// --- the budget ------------------------------------------------------------

export interface Reservation {
  wei: bigint;
  /** Set once settled or released, so a double-settle cannot refund twice. */
  closed: boolean;
}

export interface BudgetView {
  launches: number;
  spentWei: string;
  maxLaunches?: number;
  maxSpendWei?: string;
  launchesLeft?: number;
  spendLeftWei?: string;
  broken?: string;
}

/** A grant's running debits, kept on disk so a restart does not hand the
 *  tenant a fresh allowance. */
export class Budget {
  private launches = 0;
  private spentWei = 0n;
  /** Set when the ledger exists but cannot be read. A budget that cannot be
   *  read refuses to spend: the failure mode of a lost ledger must not be a
   *  reset allowance. */
  private broken?: string;

  constructor(
    private readonly path: string,
    private readonly maxLaunches?: number,
    private readonly maxSpendWei?: bigint,
  ) {
    if (!existsSync(path)) return;
    try {
      const o = JSON.parse(readFileSync(path, "utf8")) as { launches?: unknown; spentWei?: unknown };
      if (typeof o.launches !== "number" || !Number.isInteger(o.launches) || o.launches < 0) throw new Error("launches");
      if (typeof o.spentWei !== "string" || !/^\d+$/.test(o.spentWei)) throw new Error("spentWei");
      this.launches = o.launches;
      this.spentWei = BigInt(o.spentWei);
    } catch (e) {
      this.broken = `${path} cannot be read (${(e as Error).message}), so this grant will not spend`;
    }
  }

  view(): BudgetView {
    // A broken ledger reads as nothing left, because that is what it means:
    // reporting a full allowance next to "will not spend" would be a lie about
    // the one number an operator reads to decide whether to top a grant up.
    const left = (max: bigint) => (this.broken ? 0n : max > this.spentWei ? max - this.spentWei : 0n);
    return {
      launches: this.launches,
      spentWei: this.spentWei.toString(),
      maxLaunches: this.maxLaunches,
      maxSpendWei: this.maxSpendWei?.toString(),
      launchesLeft:
        this.maxLaunches === undefined ? undefined : this.broken ? 0 : Math.max(0, this.maxLaunches - this.launches),
      spendLeftWei: this.maxSpendWei === undefined ? undefined : left(this.maxSpendWei).toString(),
      broken: this.broken,
    };
  }

  /** Debit before signing. `launches` covers a loop reserving its worst case
   *  up front, which is the only honest way to bound a run whose later specs
   *  do not exist yet. */
  reserve(wei: bigint, launches = 1): { ok: true; handle: Reservation } | { ok: false; reason: string } {
    if (this.broken) return { ok: false, reason: this.broken };
    if (this.maxLaunches !== undefined && this.launches + launches > this.maxLaunches) {
      return {
        ok: false,
        reason: `this grant allows ${this.maxLaunches} launches and has used ${this.launches}; ${launches} more would exceed it`,
      };
    }
    if (this.maxSpendWei !== undefined && this.spentWei + wei > this.maxSpendWei) {
      return {
        ok: false,
        reason: `this grant allows ${this.maxSpendWei} wei of opening buys and has used ${this.spentWei}; ${wei} more would exceed it`,
      };
    }
    this.launches += launches;
    this.spentWei += wei;
    this.persist();
    return { ok: true, handle: { wei, closed: false } };
  }

  /** Close a reservation against what was actually signed for. Refunds the
   *  difference, and refunds launches that never happened. */
  settle(handle: Reservation, actualWei: bigint, actualLaunches = 1, reservedLaunches = 1): void {
    if (handle.closed) return;
    handle.closed = true;
    this.spentWei = this.spentWei - handle.wei + actualWei;
    if (this.spentWei < 0n) this.spentWei = 0n;
    this.launches = Math.max(0, this.launches - reservedLaunches + actualLaunches);
    this.persist();
  }

  /** Nothing was signed: give it all back. */
  release(handle: Reservation, reservedLaunches = 1): void {
    this.settle(handle, 0n, 0, reservedLaunches);
  }

  private persist(): void {
    try {
      mkdirSync(dirname(this.path), { recursive: true });
      // Write and rename, so a crash mid-write cannot leave a ledger that
      // reads as broken and locks the grant out.
      const tmp = `${this.path}.tmp`;
      writeFileSync(tmp, `${JSON.stringify({ launches: this.launches, spentWei: this.spentWei.toString() })}\n`);
      renameSync(tmp, this.path);
    } catch (e) {
      this.broken = `cannot write ${this.path} (${(e as Error).message}), so this grant will not spend`;
    }
  }
}

// --- the runtime -----------------------------------------------------------

export interface TenantRuntime {
  grant: TenantGrant;
  dep: LauncherDeployment;
  /** The server's run config, narrowed by the grant. Never widened. */
  run: RunConfig;
  budget: Budget;
}

function resolvePath(p: string): string {
  return isAbsolute(p) ? p : resolve(process.cwd(), p);
}

/** Load one tenant's deployment and narrow the server's config to it. */
export function tenantRuntime(grant: TenantGrant, serverRun: RunConfig): TenantRuntime {
  const dep = loadLauncherDeployment({
    tenant: grant.slug,
    chainId: grant.chainId,
    ...(grant.deployment ? { file: grant.deployment } : {}),
  });
  if (dep.chainId !== grant.chainId) {
    throw new GrantError(`${grant.slug}: the deployment is for chain ${dep.chainId}, the grant says ${grant.chainId}`);
  }
  const outDir = join(serverRun.outDir, grant.slug);
  const run: RunConfig = {
    ...serverRun,
    // Narrowing only: a grant cannot turn a dry-run server into a signing one.
    dryRun: serverRun.dryRun || !grant.maySign,
    rpcUrl: grant.rpcUrl ?? serverRun.rpcUrl,
    outDir,
  };
  return {
    grant,
    dep,
    run,
    budget: new Budget(resolvePath(grant.ledger ?? join(outDir, "budget.json")), grant.maxLaunches, grant.maxSpendWei),
  };
}
