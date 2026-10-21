/** Who may ask this service to do anything, and on whose behalf.
 *
 *  The CLI's protection was never a flag; it was that you had to be on the box
 *  with a shell. An HTTP surface throws that away, so it has to be rebuilt on
 *  purpose: everything except the liveness probe needs a bearer token, and the
 *  token belongs to the website's backend, never to a browser.
 *
 *  A token does not merely open the door, it says which tenant is at it. That
 *  is the whole reason a request may not name its own tenant: the credential
 *  already did, and a caller cannot change their credential by changing their
 *  JSON.
 *
 *  A service with no tokens configured refuses every authenticated route
 *  rather than running open. The failure mode of an unset secret should be a
 *  locked door, not an unlocked one. */

import { tokenDigest } from "./tenants.js";

export type AuthResult = { ok: true; slug: string } | { ok: false; status: 401 | 503; message: string };

export interface AuthConfig {
  /** sha256(token) -> tenant slug. Empty means the service is closed.
   *
   *  Only digests are kept: nothing needs the token again, and a digest is not
   *  a credential. Looking up by digest leaks nothing through timing either —
   *  the only thing a probe's timing could describe is the digest the caller
   *  computed themselves, and sha256 does not run backwards. */
  tokens?: Map<string, string>;
}

/** Check an Authorization header and say which tenant it speaks for. */
export function authorize(header: string | undefined, cfg: AuthConfig): AuthResult {
  if (!cfg.tokens || cfg.tokens.size === 0) {
    return {
      ok: false,
      status: 503,
      message: "no tenant tokens are configured, so every authenticated route is closed.",
    };
  }
  const presented = /^Bearer (.+)$/i.exec(header ?? "")?.[1]?.trim();
  if (!presented) return { ok: false, status: 401, message: "an Authorization: Bearer <token> header is required" };
  const slug = cfg.tokens.get(tokenDigest(presented));
  if (!slug) return { ok: false, status: 401, message: "that token is not valid" };
  return { ok: true, slug };
}

/** Build the token table from grants. */
export function authConfig(grants: { grant: { slug: string }; token: string }[]): AuthConfig {
  const tokens = new Map<string, string>();
  for (const g of grants) tokens.set(tokenDigest(g.token), g.grant.slug);
  return { tokens };
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
