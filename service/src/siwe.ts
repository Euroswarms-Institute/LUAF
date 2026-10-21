/** Signing in to the console with a wallet.
 *
 *  The console used to live in a tenant's own website, with a worker in front
 *  holding the grant token. That website is not ours to change, so the sign-in
 *  moves here and one console serves every tenant — which is the better shape
 *  anyway: adding a launchpad no longer means editing a launchpad's site.
 *
 *  The thing to get right is that a browser must never hold a grant token. A
 *  grant token is long-lived, has no owner, and opens every route; it belongs
 *  to a backend. So an operator proves a wallet instead and receives a short,
 *  scoped session bound to one tenant. That is the pattern the agent
 *  credential-vault work argues for (arXiv:2604.23280): the weakest holder
 *  gets a short-lived token scoped to what it is doing, never the standing
 *  credential.
 *
 *  The session carries the grant's authority and no more, and the grant's
 *  budget is what bounds the damage if one is stolen: an operator's session can
 *  still only spend what that grant allows. */

import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { verifyMessage } from "viem";

import type { TenantGrant } from "./tenants.js";

/** How far a signed timestamp may sit from our clock. */
export const SKEW_SECS = 5 * 60;

/** The exact text a wallet signs.
 *
 *  It names the launchpad and says plainly that signing in authorises launches
 *  that spend ETH. A wallet owner reads this before signing, so it has to be
 *  true and specific: a generic "sign in" would be consent to nothing in
 *  particular. */
export function consoleAuthMessage(args: {
  address: string;
  tenant: string;
  issuedAt: string;
  nonce: string;
}): string {
  return [
    `Sign in to the ${args.tenant} launch console.`,
    "",
    `Wallet: ${args.address.toLowerCase()}`,
    `Launchpad: ${args.tenant}`,
    `Issued at: ${args.issuedAt}`,
    `Nonce: ${args.nonce}`,
    "",
    "Signing in lets this wallet design and launch coins on this launchpad,",
    "which spends the operator's ETH on opening buys, within the launchpad's",
    "own spending limit. This signature cannot move funds by itself.",
  ].join("\n");
}

const ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const NONCE = /^[0-9a-f]{16,64}$/;
const SIGNATURE = /^0x[0-9a-fA-F]{130}$/;

export interface SessionConfig {
  /** HMAC key for session tokens. Without it, console sign-in is closed. */
  secret?: string;
  /** How long a session lasts. Short on purpose: it is held by a browser. */
  ttlSecs: number;
}

export function sessionConfigFromEnv(): SessionConfig {
  const hours = process.env.LUCRE_SESSION_HOURS?.trim();
  if (hours && !/^\d+$/.test(hours)) throw new Error(`LUCRE_SESSION_HOURS=${hours}: expected an integer`);
  const h = hours ? Number(hours) : 8;
  if (h < 1 || h > 24 * 7) throw new Error("LUCRE_SESSION_HOURS must be from 1 to 168");
  return { secret: process.env.LUCRE_SESSION_SECRET?.trim() || undefined, ttlSecs: h * 3600 };
}

/** Shortest secret that will sign a session. A short one signs sessions anyone
 *  can forge, which is worse than no console at all. */
export const MIN_SECRET_CHARS = 32;

function mac(secret: string, payload: string): string {
  // The scope is signed rather than carried, so nothing else this secret ever
  // signs can be presented here.
  return createHmac("sha256", secret).update(`console:${payload}`).digest("base64url");
}

export interface Session {
  address: string;
  tenant: string;
  expires: number;
}

export function issueSession(cfg: SessionConfig, address: string, tenant: string, nowSecs: number): {
  token: string;
  expires: number;
} {
  if (!cfg.secret) throw new Error("LUCRE_SESSION_SECRET is not set");
  const expires = nowSecs + cfg.ttlSecs;
  const payload = `${address.toLowerCase()}.${tenant}.${expires}`;
  return { token: `${payload}.${mac(cfg.secret, payload)}`, expires };
}

/** The session a token stands for, or null if forged, expired or malformed. */
export function readSession(cfg: SessionConfig, token: string, nowSecs: number): Session | null {
  if (!cfg.secret) return null;
  const parts = token.split(".");
  if (parts.length !== 4) return null;
  const [address, tenant, exp, presented] = parts as [string, string, string, string];
  if (!ADDRESS.test(address) || !/^[a-z0-9][a-z0-9-]{0,62}$/.test(tenant)) return null;
  if (!/^\d+$/.test(exp) || Number(exp) < nowSecs) return null;
  const want = mac(cfg.secret, `${address}.${tenant}.${exp}`);
  const a = Buffer.from(want);
  const b = Buffer.from(presented);
  if (a.length !== b.length) {
    timingSafeEqual(a, a);
    return null;
  }
  if (!timingSafeEqual(a, b)) return null;
  return { address: address.toLowerCase(), tenant, expires: Number(exp) };
}

/** Signatures already spent.
 *
 *  Kept in memory, which is enough: a signature is only usable inside the skew
 *  window, so a restart can at worst re-open a five-minute door for a
 *  signature that was already presented once — and the window is itself what
 *  stops a copied signature being useful tomorrow. */
export class Nonces {
  private readonly seen = new Map<string, number>();

  constructor(private readonly ttlSecs = SKEW_SECS * 3) {}

  /** True if this nonce has been used. Records it either way. */
  spend(nonce: string, nowSecs: number): boolean {
    this.prune(nowSecs);
    if (this.seen.has(nonce)) return true;
    this.seen.set(nonce, nowSecs + this.ttlSecs);
    return false;
  }

  private prune(nowSecs: number): void {
    for (const [k, exp] of this.seen) if (exp <= nowSecs) this.seen.delete(k);
  }
}

export function newNonce(): string {
  return randomUUID().replace(/-/g, "");
}

export type SignInResult =
  | { ok: true; token: string; expires: number; tenant: string; address: string }
  | { ok: false; status: 400 | 401 | 403 | 503; error: string };

/** Verify a wallet's proof and issue a session for one grant.
 *
 *  The request names the launchpad, which is not a way in: the grant's own
 *  operator list decides whether this wallet may sign in for it. Naming it is
 *  only how a wallet that operates two launchpads says which one it means. */
export async function signIn(args: {
  body: unknown;
  grants: Map<string, TenantGrant>;
  session: SessionConfig;
  nonces: Nonces;
  nowSecs: number;
}): Promise<SignInResult> {
  const { session, nowSecs } = args;
  if (!session.secret) {
    return { ok: false, status: 503, error: "console sign-in is closed: LUCRE_SESSION_SECRET is not set" };
  }
  if (session.secret.length < MIN_SECRET_CHARS) {
    return {
      ok: false,
      status: 503,
      error: `console sign-in is closed: LUCRE_SESSION_SECRET is only ${session.secret.length} characters`,
    };
  }
  const o = (typeof args.body === "object" && args.body !== null ? args.body : {}) as Record<string, unknown>;
  const { address, tenant, issuedAt, nonce, signature } = o;
  if (
    typeof address !== "string" ||
    !ADDRESS.test(address) ||
    typeof tenant !== "string" ||
    typeof issuedAt !== "string" ||
    typeof nonce !== "string" ||
    !NONCE.test(nonce) ||
    typeof signature !== "string" ||
    !SIGNATURE.test(signature)
  ) {
    return { ok: false, status: 400, error: "send address, tenant, issuedAt, nonce and signature" };
  }

  const grant = args.grants.get(tenant);
  // Checked before the signature work: a wallet that cannot operate this
  // launchpad should be told so, not asked to prove it owns something useless.
  // The same answer for an unknown launchpad and a wallet that is not an
  // operator, so neither can be used to enumerate the other.
  if (!grant || !(grant.operators ?? []).includes(address.toLowerCase())) {
    return { ok: false, status: 403, error: "that wallet is not an operator of that launchpad" };
  }

  const t = Date.parse(issuedAt);
  if (!Number.isFinite(t) || Math.abs(t / 1000 - nowSecs) > SKEW_SECS) {
    return { ok: false, status: 400, error: "that signature is too old; sign again" };
  }
  if (args.nonces.spend(nonce, nowSecs)) {
    return { ok: false, status: 401, error: "that signature has already been used; sign again" };
  }

  const good = await verifyMessage({
    address: address as `0x${string}`,
    message: consoleAuthMessage({ address, tenant, issuedAt, nonce }),
    signature: signature as `0x${string}`,
  }).catch(() => false);
  if (!good) return { ok: false, status: 401, error: "that signature does not match" };

  const { token, expires } = issueSession(session, address, tenant, nowSecs);
  return { ok: true, token, expires, tenant, address: address.toLowerCase() };
}
