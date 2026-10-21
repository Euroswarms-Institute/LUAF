import { describe, expect, it } from "vitest";

import { authorize, type AuthConfig } from "./auth.js";
import { Jobs } from "./jobs.js";
import { parseRunRequest, specFromRequest } from "./request.js";
import { resolveLive, serverCanSign } from "./routes.js";
import type { RunConfig } from "@lucreocracy/core";

const TOKEN = "a".repeat(32);
const auth: AuthConfig = { token: TOKEN };

const runCfg = (over: Partial<RunConfig> = {}): RunConfig => ({
  dryRun: true,
  rpcUrl: "https://rpc.example.invalid",
  maxSteps: 3,
  minLines: 400,
  validationTimeoutMs: 1000,
  outDir: "runs",
  ...over,
});

const KEY = `0x${"1".repeat(64)}` as const;

describe("authorisation", () => {
  it("accepts the configured token", () => {
    expect(authorize(`Bearer ${TOKEN}`, auth)).toEqual({ ok: true });
    expect(authorize(`bearer ${TOKEN}`, auth).ok).toBe(true);
  });

  it("rejects a missing, malformed or wrong token", () => {
    expect(authorize(undefined, auth)).toMatchObject({ ok: false, status: 401 });
    expect(authorize("", auth)).toMatchObject({ ok: false, status: 401 });
    expect(authorize(TOKEN, auth)).toMatchObject({ ok: false, status: 401 });
    expect(authorize(`Bearer ${"b".repeat(32)}`, auth)).toMatchObject({ ok: false, status: 401 });
    // A prefix of the real token must not pass.
    expect(authorize(`Bearer ${TOKEN.slice(0, 20)}`, auth)).toMatchObject({ ok: false, status: 401 });
  });

  it("closes every authenticated route when no token is configured", () => {
    // An unset secret must fail shut, not open.
    const verdict = authorize(`Bearer ${TOKEN}`, {});
    expect(verdict).toMatchObject({ ok: false, status: 503 });
    expect(verdict.ok === false && verdict.message).toMatch(/LUCRE_API_TOKEN is not set/);
  });
});

describe("the two keys that have to turn before anything signs", () => {
  it("needs both the server's configuration and the request", () => {
    const signing = runCfg({ dryRun: false, privateKey: KEY });
    expect(serverCanSign(signing)).toBe(true);
    expect(resolveLive({ live: true, designOnly: false }, signing)).toEqual({ live: true });
  });

  it("will not let a request talk a dry-run server into signing", () => {
    const verdict = resolveLive({ live: true, designOnly: false }, runCfg());
    expect(verdict.live).toBe(false);
    expect(verdict.refusal).toMatch(/cannot grant itself signing authority/);
  });

  it("will not sign without a key even when dry run is off", () => {
    // Should be unreachable through runConfig, which refuses this pairing, but
    // the route must not depend on that to be safe.
    const verdict = resolveLive({ live: true, designOnly: false }, runCfg({ dryRun: false }));
    expect(verdict.live).toBe(false);
    expect(verdict.refusal).toBeDefined();
  });

  it("is a dry run when the request does not ask, on a server that could", () => {
    const signing = runCfg({ dryRun: false, privateKey: KEY });
    // A website bug that forgets the field costs nothing.
    expect(resolveLive({ live: false, designOnly: false }, signing)).toEqual({ live: false });
  });

  it("defaults an absent field to a dry run", () => {
    const parsed = parseRunRequest("{}");
    expect(parsed.ok && parsed.value.live).toBe(false);
  });
});

describe("what a caller may ask for", () => {
  it("takes a brief, a profile and launch terms", () => {
    const parsed = parseRunRequest(
      JSON.stringify({ profile: "crypto", topic: "an arbitrage bot", firstBuyWei: "1000000000000000", live: true }),
    );
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    expect(parsed.value.profile).toBe("crypto");
    expect(parsed.value.firstBuyWei).toBe(1_000_000_000_000_000n);
    expect(parsed.value.live).toBe(true);
  });

  it("refuses the fields the server owns, and names them", () => {
    for (const field of ["rpcUrl", "privateKey", "outDir", "tenant", "chainId", "dryRun"]) {
      const parsed = parseRunRequest(JSON.stringify({ [field]: "x" }));
      expect(parsed.ok, field).toBe(false);
      if (!parsed.ok) expect(parsed.error).toContain(field);
    }
  });

  it("refuses a wei amount sent as a number, rather than rounding it", () => {
    const parsed = parseRunRequest(JSON.stringify({ firstBuyWei: 1e18 }));
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toMatch(/decimal string/);
  });

  it("rejects a profile that is not a plain identifier", () => {
    for (const p of ["../etc", "Crypto", "a b", "x/y"]) {
      expect(parseRunRequest(JSON.stringify({ profile: p })).ok, p).toBe(false);
    }
  });

  it("bounds the brief and rejects a non-object body", () => {
    expect(parseRunRequest(JSON.stringify({ topic: "x".repeat(2_001) })).ok).toBe(false);
    expect(parseRunRequest("[1,2]").ok).toBe(false);
    expect(parseRunRequest("not json").ok).toBe(false);
    expect(parseRunRequest("").ok).toBe(true); // an empty body is a default run
  });

  it("validates the tax buckets it is given", () => {
    const bad = parseRunRequest(JSON.stringify({ tax: { devBps: 99_999 } }));
    expect(bad.ok).toBe(false);
    const good = parseRunRequest(JSON.stringify({ tax: { buyTaxBps: 100 } }));
    expect(good.ok).toBe(true);
  });
});

describe("building the spec from a request", () => {
  const server = { tenant: "acme", chainId: 4663, defaultProfile: "crypto" };

  it("takes the tenant and chain from the server, never the caller", () => {
    const parsed = parseRunRequest(JSON.stringify({ topic: "a thing" }));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const spec = specFromRequest(parsed.value, server);
    expect(spec.ok).toBe(true);
    if (!spec.ok) return;
    expect(spec.value.tenant).toBe("acme");
    expect(spec.value.chainId).toBe(4663);
    expect(spec.value.topic).toBe("a thing");
  });

  it("rejects a spec the factory would reject, before anything is signed", () => {
    const parsed = parseRunRequest(JSON.stringify({ tax: { devBps: 1 } }));
    expect(parsed.ok).toBe(true);
    if (!parsed.ok) return;
    const spec = specFromRequest(parsed.value, server);
    expect(spec.ok).toBe(false);
    if (spec.ok) return;
    expect(spec.error).toMatch(/sum to 10000/);
  });

  it("falls back to the server's default profile", () => {
    const spec = specFromRequest({ live: false, designOnly: false }, server);
    expect(spec.ok && spec.value.profile).toBe("crypto");
  });
});

describe("runs as jobs", () => {
  const settle = () => new Promise((r) => setTimeout(r, 10));

  it("returns immediately and reports the result when it lands", async () => {
    const jobs = new Jobs();
    const job = jobs.start({
      live: false,
      run: async ({ log }) => {
        log("designed");
        return {
          specId: "0xabc",
          brief: "a brief",
          product: { name: "Agent", ticker: "AGT" },
          lines: 420,
          dir: "runs/x",
          publish: { dryRun: true, token: "0x2add" },
          bundle: { dir: "runs/x/endpoint" },
        } as never;
      },
    });
    expect(job.state).toBe("running");
    await settle();
    const view = jobs.view(jobs.get(job.id)!);
    expect(view.state).toBe("done");
    expect(view.result?.ticker).toBe("AGT");
    expect(view.result?.dryRun).toBe(true);
    expect(view.log).toContain("designed");
  });

  it("records a failure as failed, with its reason", async () => {
    const jobs = new Jobs();
    const job = jobs.start({ live: false, run: async () => { throw new Error("no usable design"); } });
    await settle();
    const view = jobs.view(jobs.get(job.id)!);
    expect(view.state).toBe("failed");
    expect(view.error).toBe("no usable design");
  });

  it("cancels cooperatively and says so", async () => {
    const jobs = new Jobs();
    const job = jobs.start({
      live: false,
      run: ({ signal }) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("aborted")));
        }),
    });
    expect(jobs.cancel(job.id)).toBe(true);
    await settle();
    expect(jobs.get(job.id)!.state).toBe("cancelled");
    // A finished job cannot be cancelled again.
    expect(jobs.cancel(job.id)).toBe(false);
    expect(jobs.cancel("nope")).toBe(false);
  });

  it("lists newest first and forgets the oldest", async () => {
    const jobs = new Jobs(3);
    const ids: string[] = [];
    for (let i = 0; i < 5; i++) {
      ids.push(jobs.start({ live: false, run: async () => ({}) as never }).id);
    }
    await settle();
    const list = jobs.list(10);
    expect(list).toHaveLength(3);
    expect(list[0]!.id).toBe(ids[4]);
    expect(jobs.get(ids[0]!)).toBeUndefined();
  });

  it("marks a live job as live, so the record shows what signed", async () => {
    const jobs = new Jobs();
    const job = jobs.start({ live: true, run: async () => ({}) as never });
    expect(job.live).toBe(true);
    await settle();
    expect(jobs.get(job.id)!.live).toBe(true);
  });
});
