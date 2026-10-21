/** Who may ask this service to do anything.
 *
 *  The CLI's protection was never a flag; it was that you had to be on the box
 *  with a shell. An HTTP surface throws that away, so it has to be rebuilt on
 *  purpose: everything except the liveness probe needs a bearer token, and the
 *  token belongs to the website's backend, never to a browser.
 *
 *  A service with no token configured refuses every authenticated route rather
 *  than running open. The failure mode of an unset secret should be a locked
 *  door, not an unlocked one. */

import { timingSafeEqual } from "node:crypto";

export type AuthResult = { ok: true } | { ok: false; status: 401 | 503; message: string };

/** Compare without leaking length or content through timing. */
function sameToken(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  // timingSafeEqual throws on a length mismatch, which would itself be an
  // oracle. Hash-free fix: compare equal-length buffers and the lengths apart.
  if (left.length !== right.length) {
    // Still do a comparison so the work done does not depend on the mismatch.
    timingSafeEqual(left, left);
    return false;
  }
  return timingSafeEqual(left, right);
}

export interface AuthConfig {
  /** The expected bearer token, or undefined when none is configured. */
  token?: string;
}

export function authConfigFromEnv(): AuthConfig {
  const token = process.env.LUCRE_API_TOKEN?.trim();
  return { token: token || undefined };
}

/** Check an Authorization header against the configured token. */
export function authorize(header: string | undefined, cfg: AuthConfig): AuthResult {
  if (!cfg.token) {
    return {
      ok: false,
      status: 503,
      message: "LUCRE_API_TOKEN is not set, so every authenticated route is closed. Set it to open the service.",
    };
  }
  const presented = /^Bearer (.+)$/i.exec(header ?? "")?.[1]?.trim();
  if (!presented) return { ok: false, status: 401, message: "an Authorization: Bearer <token> header is required" };
  if (!sameToken(presented, cfg.token)) return { ok: false, status: 401, message: "that token is not valid" };
  return { ok: true };
}

/** A token short enough to be a placeholder is a mistake worth naming at
 *  startup rather than discovering later. */
export function warnWeakToken(cfg: AuthConfig, log: (s: string) => void): void {
  if (!cfg.token) {
    log("LUCRE_API_TOKEN is unset: the service will refuse every route but /health.");
    return;
  }
  if (cfg.token.length < 24) {
    log(`LUCRE_API_TOKEN is only ${cfg.token.length} characters. Use at least 24 random ones.`);
  }
}
