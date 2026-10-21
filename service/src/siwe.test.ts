import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import { privateKeyToAccount } from "viem/accounts";

import {
  consoleAuthMessage,
  issueSession,
  newNonce,
  Nonces,
  readSession,
  sessionConfigFromEnv,
  signIn,
  SKEW_SECS,
  type SessionConfig,
} from "./siwe.js";
import type { TenantGrant } from "./tenants.js";

const NOW = 1_800_000_000;
const session: SessionConfig = { secret: "s".repeat(32), ttlSecs: 8 * 3600 };
const account = privateKeyToAccount(`0x${"7".repeat(64)}`);
const OP = account.address.toLowerCase();
const OTHER = "0x1111111111111111111111111111111111111111";

const grant = (over: Partial<TenantGrant> = {}): TenantGrant => ({
  slug: "acme",
  chainId: 4663,
  maySign: true,
  operators: [OP],
  ...over,
});

const grants = (...list: TenantGrant[]) => new Map(list.map((g) => [g.slug, g]));

/** The exact text a wallet is asked to sign.
 *
 *  Written out in full rather than built from the function, because the browser
 *  builds the same string by hand in service/public/console.html. If either
 *  side is edited alone this fails, instead of every sign-in failing once it
 *  ships. */
const EXPECTED = `Sign in to the acme launch console.

Wallet: 0x1111111111111111111111111111111111111111
Launchpad: acme
Issued at: 2026-01-01T00:00:00.000Z
Nonce: a1b2c3d4e5f60718

Signing in lets this wallet design and launch coins on this launchpad,
which spends the operator's ETH on opening buys, within the launchpad's
own spending limit. This signature cannot move funds by itself.`;

describe("the message a wallet signs", () => {
  it("is exactly what the page builds", () => {
    expect(
      consoleAuthMessage({
        address: OTHER,
        tenant: "acme",
        issuedAt: "2026-01-01T00:00:00.000Z",
        nonce: "a1b2c3d4e5f60718",
      }),
    ).toBe(EXPECTED);
  });

  it("names the launchpad and says what signing in pays for", () => {
    const m = consoleAuthMessage({ address: OP, tenant: "zenith", issuedAt: "t", nonce: "n" });
    expect(m).toContain("Sign in to the zenith launch console.");
    expect(m).toContain("spends the operator's ETH");
    expect(m).toContain("cannot move funds by itself");
  });

  it("is built the same way by the page, which nothing else typechecks", () => {
    // The console page is plain JavaScript served as a file, so the compiler
    // never sees it. Lift its own authMessage out and run it: if the two drift
    // apart, every sign-in fails with "that signature does not match" and
    // nothing says why. This is the test that says why.
    const here = dirname(fileURLToPath(import.meta.url));
    const html = readFileSync(join(here, "../public/console.html"), "utf8");
    const match = /function authMessage\(address, tenant, issuedAt, nonce\) \{[\s\S]*?\n\}/.exec(html);
    expect(match, "the page's authMessage could not be found").not.toBeNull();
    const fromPage = new Function(`${match![0]}; return authMessage;`)() as (
      a: string,
      t: string,
      i: string,
      n: string,
    ) => string;
    for (const [address, tenant] of [
      [OTHER, "acme"],
      [account.address, "zenith-two"],
    ] as const) {
      expect(fromPage(address, tenant, "2026-01-01T00:00:00.000Z", "a1b2c3d4e5f60718")).toBe(
        consoleAuthMessage({ address, tenant, issuedAt: "2026-01-01T00:00:00.000Z", nonce: "a1b2c3d4e5f60718" }),
      );
    }
  });

  it("gives a fresh nonce each time", () => {
    const seen = new Set(Array.from({ length: 50 }, newNonce));
    expect(seen.size).toBe(50);
    for (const n of seen) expect(n).toMatch(/^[0-9a-f]{32}$/);
  });
});

describe("sessions", () => {
  it("reads back the wallet and the launchpad it was issued for", () => {
    const { token, expires } = issueSession(session, OP.toUpperCase(), "acme", NOW);
    expect(expires).toBe(NOW + 8 * 3600);
    expect(readSession(session, token, NOW + 1)).toEqual({ address: OP, tenant: "acme", expires });
  });

  it("is not valid for another launchpad, however it is edited", () => {
    const { token } = issueSession(session, OP, "acme", NOW);
    const [addr, , exp, mac] = token.split(".");
    // Swapping the tenant invalidates the mac, so a session for one launchpad
    // cannot be spent on another.
    expect(readSession(session, `${addr}.zenith.${exp}.${mac}`, NOW + 1)).toBeNull();
  });

  it("expires, and will not read under another secret", () => {
    const { token } = issueSession(session, OP, "acme", NOW);
    expect(readSession(session, token, NOW + 8 * 3600 + 1)).toBeNull();
    expect(readSession({ ...session, secret: "x".repeat(32) }, token, NOW + 1)).toBeNull();
  });

  it("refuses a malformed token and a service with no secret", () => {
    for (const t of ["", "garbage", "a.b.c", "a.b.c.d.e"]) {
      expect(readSession(session, t, NOW), t).toBeNull();
    }
    const { token } = issueSession(session, OP, "acme", NOW);
    expect(readSession({ ttlSecs: 3600 }, token, NOW + 1)).toBeNull();
  });

  it("will not issue one without a secret", () => {
    expect(() => issueSession({ ttlSecs: 3600 }, OP, "acme", NOW)).toThrow(/LUCRE_SESSION_SECRET/);
  });
});

describe("spent signatures", () => {
  it("accepts a nonce once", () => {
    const n = new Nonces();
    expect(n.spend("abc", NOW)).toBe(false);
    expect(n.spend("abc", NOW)).toBe(true);
  });

  it("forgets one past its window, which the timestamp check then catches", () => {
    const n = new Nonces(60);
    expect(n.spend("abc", NOW)).toBe(false);
    expect(n.spend("abc", NOW + 61)).toBe(false);
  });
});

describe("signing in", () => {
  const nonce = "a1b2c3d4e5f60718";
  const issuedAt = new Date(NOW * 1000).toISOString();

  async function attempt(over: Record<string, unknown> = {}, opts: { session?: SessionConfig; grants?: Map<string, TenantGrant>; nonces?: Nonces } = {}) {
    const address = (over.address as string) ?? account.address;
    const tenant = (over.tenant as string) ?? "acme";
    const at = (over.issuedAt as string) ?? issuedAt;
    const n = (over.nonce as string) ?? nonce;
    const signature =
      (over.signature as string) ??
      (await account.signMessage({ message: consoleAuthMessage({ address, tenant, issuedAt: at, nonce: n }) }));
    return signIn({
      body: { address, tenant, issuedAt: at, nonce: n, signature, ...over },
      grants: opts.grants ?? grants(grant()),
      session: opts.session ?? session,
      nonces: opts.nonces ?? new Nonces(),
      nowSecs: NOW,
    });
  }

  it("issues a session to an operator who proves the wallet", async () => {
    const r = await attempt();
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.tenant).toBe("acme");
    expect(r.address).toBe(OP);
    expect(readSession(session, r.token, NOW + 1)).toMatchObject({ address: OP, tenant: "acme" });
  });

  it("gives the same answer for a wallet that is not an operator and a launchpad that does not exist", async () => {
    // Otherwise either could be used to enumerate the other.
    const notOperator = await attempt({}, { grants: grants(grant({ operators: [OTHER] })) });
    const noSuchTenant = await attempt({ tenant: "nowhere" });
    expect(notOperator).toMatchObject({ ok: false, status: 403 });
    expect(noSuchTenant).toMatchObject({ ok: false, status: 403 });
    expect(notOperator.ok === false && notOperator.error).toBe(noSuchTenant.ok === false && noSuchTenant.error);
  });

  it("refuses a grant with no operators at all", async () => {
    const r = await attempt({}, { grants: grants(grant({ operators: undefined })) });
    expect(r).toMatchObject({ ok: false, status: 403 });
  });

  it("refuses a stale signature", async () => {
    const old = new Date((NOW - SKEW_SECS - 1) * 1000).toISOString();
    expect(await attempt({ issuedAt: old })).toMatchObject({ ok: false, status: 400 });
    // And one from the future, which is the same mistake in the other direction.
    const ahead = new Date((NOW + SKEW_SECS + 1) * 1000).toISOString();
    expect(await attempt({ issuedAt: ahead })).toMatchObject({ ok: false, status: 400 });
  });

  it("refuses a replayed signature", async () => {
    const nonces = new Nonces();
    expect((await attempt({}, { nonces })).ok).toBe(true);
    const again = await attempt({}, { nonces });
    expect(again).toMatchObject({ ok: false, status: 401 });
    if (!again.ok) expect(again.error).toMatch(/already been used/);
  });

  it("refuses a signature over different text", async () => {
    // Signed for another launchpad, presented for this one.
    const signature = await account.signMessage({
      message: consoleAuthMessage({ address: account.address, tenant: "zenith", issuedAt, nonce }),
    });
    expect(await attempt({ signature })).toMatchObject({ ok: false, status: 401 });
  });

  it("refuses a signature from another wallet", async () => {
    const other = privateKeyToAccount(`0x${"8".repeat(64)}`);
    const signature = await other.signMessage({
      message: consoleAuthMessage({ address: account.address, tenant: "acme", issuedAt, nonce }),
    });
    expect(await attempt({ signature })).toMatchObject({ ok: false, status: 401 });
  });

  it("refuses a malformed body", async () => {
    for (const over of [{ address: "nope" }, { nonce: "xyz" }, { signature: "0x00" }, { tenant: 5 }]) {
      expect(await attempt(over as Record<string, unknown>), JSON.stringify(over)).toMatchObject({
        ok: false,
        status: 400,
      });
    }
    expect(await signIn({ body: null, grants: grants(grant()), session, nonces: new Nonces(), nowSecs: NOW })).toMatchObject({
      ok: false,
      status: 400,
    });
  });

  it("is closed, not open, when no session secret is set", async () => {
    expect(await attempt({}, { session: { ttlSecs: 3600 } })).toMatchObject({ ok: false, status: 503 });
    // A short secret signs sessions anyone can forge, which is worse than no
    // console at all.
    expect(await attempt({}, { session: { secret: "short", ttlSecs: 3600 } })).toMatchObject({ ok: false, status: 503 });
  });
});

describe("session configuration", () => {
  const withEnv = <T,>(vars: Record<string, string | undefined>, fn: () => T): T => {
    const saved: Record<string, string | undefined> = {};
    for (const [k, v] of Object.entries(vars)) {
      saved[k] = process.env[k];
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    try {
      return fn();
    } finally {
      for (const [k, v] of Object.entries(saved)) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
      }
    }
  };

  it("lasts eight hours unless told otherwise", () => {
    expect(withEnv({ LUCRE_SESSION_HOURS: undefined, LUCRE_SESSION_SECRET: "s" }, sessionConfigFromEnv)).toEqual({
      secret: "s",
      ttlSecs: 8 * 3600,
    });
    expect(withEnv({ LUCRE_SESSION_HOURS: "2" }, sessionConfigFromEnv).ttlSecs).toBe(7200);
  });

  it("refuses a nonsensical or unbounded lifetime", () => {
    for (const h of ["0", "169", "abc", "-1"]) {
      expect(() => withEnv({ LUCRE_SESSION_HOURS: h }, sessionConfigFromEnv), h).toThrow();
    }
  });

  it("treats an empty secret as unset, because CI passes unset secrets as empty", () => {
    expect(withEnv({ LUCRE_SESSION_SECRET: "  " }, sessionConfigFromEnv).secret).toBeUndefined();
  });
});
