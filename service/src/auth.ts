/** Who may ask this service to do anything, and on whose behalf.
 *
 *  The CLI's protection was never a flag; it was that you had to be on the box
 *  with a shell. An HTTP surface throws that away, so it has to be rebuilt on
 *  purpose: everything except the liveness probe needs a credential, and the
 *  credential says which tenant is at the door. That is the whole reason a
 *  request may not name its own tenant — the credential already did, and a
 *  caller cannot change their credential by changing their JSON.
 *
 *  There are two kinds, and they are deliberately not equal:
 *
 *    a grant token    long-lived, ownerless, reaches every route. It belongs
 *                     to a backend and never to a browser.
 *    a console session short, bound to one wallet and one tenant, and reaches
 *                     only the routes a console needs. A browser may hold it.
 *
 *  A service with neither configured refuses every authenticated route rather
 *  than running open. The failure mode of an unset secret should be a locked
 *  door, not an unlocked one. */

import { readSession, type SessionConfig } from "./siwe.js";
import { tokenDigest } from "./tenants.js";

/** What a credential turned out to be. `operator` is set only for a console
 *  session, which is what the route table uses to decide how far it reaches. */
export interface Principal {
  slug: string;
  kind: "grant" | "console";
  operator?: string;
}

export type AuthResult = { ok: true; principal: Principal } | { ok: false; status: 401 | 503; message: string };

export interface AuthConfig {
  /** sha256(token) -> tenant slug. Empty means no grant token opens anything.
   *
   *  Only digests are kept: nothing needs the token again, and a digest is not
   *  a credential. Looking up by digest leaks nothing through timing either —
   *  the only thing a probe's timing could describe is the digest the caller
   *  computed themselves, and sha256 does not run backwards. */
  tokens?: Map<string, string>;
  /** Session signing, for console credentials. */
  session?: SessionConfig;
}

/** Check an Authorization header and say who it speaks for. */
export function authorize(header: string | undefined, cfg: AuthConfig, nowSecs: number): AuthResult {
  const hasGrants = (cfg.tokens?.size ?? 0) > 0;
  const hasSessions = !!cfg.session?.secret;
  if (!hasGrants && !hasSessions) {
    return {
      ok: false,
      status: 503,
      message: "no credentials are configured, so every authenticated route is closed.",
    };
  }
  const presented = /^Bearer (.+)$/i.exec(header ?? "")?.[1]?.trim();
  if (!presented) return { ok: false, status: 401, message: "an Authorization: Bearer <token> header is required" };

  const slug = cfg.tokens?.get(tokenDigest(presented));
  if (slug) return { ok: true, principal: { slug, kind: "grant" } };

  if (cfg.session) {
    const session = readSession(cfg.session, presented, nowSecs);
    if (session) {
      return { ok: true, principal: { slug: session.tenant, kind: "console", operator: session.address } };
    }
  }
  return { ok: false, status: 401, message: "that credential is not valid" };
}

/** Build the token table from grants. */
export function authConfig(
  grants: { grant: { slug: string }; token: string }[],
  session?: SessionConfig,
): AuthConfig {
  const tokens = new Map<string, string>();
  for (const g of grants) tokens.set(tokenDigest(g.token), g.grant.slug);
  return { tokens, session };
}

/** A token short enough to be a placeholder is a mistake worth naming at
 *  startup rather than discovering later. */
export function warnWeakTokens(
  grants: { grant: { slug: string }; token: string }[],
  log: (s: string) => void,
): void {
  if (!grants.length) {
    log("no tenant tokens are configured: the service will refuse every route but /health.");
    return;
  }
  for (const g of grants) {
    if (g.token.length < 32) {
      log(`${g.grant.slug}: its token is only ${g.token.length} characters. Use at least 32 random ones.`);
    }
  }
}
