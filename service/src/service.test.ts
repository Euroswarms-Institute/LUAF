import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { authConfig, authorize, warnWeakTokens, type AuthConfig } from "./auth.js";
import { issueSession } from "./siwe.js";
import { Jobs } from "./jobs.js";
import { parseRunRequest, specFromRequest } from "./request.js";
import { resolveLive, serverCanSign } from "./routes.js";
import { broadcast, spentByLoop, spentByRun, worstCasePerLaunch } from "./spend.js";
import { Budget, GrantError, parseGrants, reloadOperators, tenantRuntime, type TenantRuntime } from "./tenants.js";
import { baseSpec, type RunConfig } from "@lucreocracy/core";

const TOKEN = "a".repeat(40);
const OTHER = "b".repeat(40);
const NOW = 1_800_000_000;
const auth: AuthConfig = authConfig([{ grant: { slug: "acme" }, token: TOKEN }]);

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
const ADDR = (n: number) => `0x${n.toString(16).padStart(40, "0")}`;

function tmp(): string {
  return mkdtempSync(join(tmpdir(), "lucre-"));
}

/** A launcher deployment on disk, since a grant resolves to a real one. */
function deploymentFile(dir: string, chainId = 4663): string {
  const path = join(dir, "launcher.json");
  writeFileSync(
    path,
    JSON.stringify({
      chainId,
      tenant: "acme",
      operator: ADDR(1),
      startBlock: 1,
      contracts: {
        launcher: ADDR(2),
        creatorSplitter: ADDR(3),
        factory: ADDR(4),
        hook: ADDR(5),
        router: ADDR(6),
        tokenDeployer: ADDR(7),
        poolManager: ADDR(8),
        weth: ADDR(9),
        platformSplitter: ADDR(10),
      },
    }),
  );
  return path;
}

describe("authorisation", () => {
  it("says which tenant a token speaks for, and that it is a grant", () => {
    expect(authorize(`Bearer ${TOKEN}`, auth, NOW)).toEqual({ ok: true, principal: { slug: "acme", kind: "grant" } });
    expect(authorize(`bearer ${TOKEN}`, auth, NOW).ok).toBe(true);
  });

  it("keeps two tenants' tokens apart", () => {
    const two = authConfig([
      { grant: { slug: "acme" }, token: TOKEN },
      { grant: { slug: "zenith" }, token: OTHER },
    ]);
    const a = authorize(`Bearer ${TOKEN}`, two, NOW);
    const z = authorize(`Bearer ${OTHER}`, two, NOW);
    expect(a.ok && a.principal.slug).toBe("acme");
    expect(z.ok && z.principal.slug).toBe("zenith");
  });

  it("rejects a missing, malformed or wrong token", () => {
    expect(authorize(undefined, auth, NOW)).toMatchObject({ ok: false, status: 401 });
    expect(authorize("", auth, NOW)).toMatchObject({ ok: false, status: 401 });
    expect(authorize(TOKEN, auth, NOW)).toMatchObject({ ok: false, status: 401 });
    expect(authorize(`Bearer ${OTHER}`, auth, NOW)).toMatchObject({ ok: false, status: 401 });
    // A prefix of the real token must not pass.
    expect(authorize(`Bearer ${TOKEN.slice(0, 20)}`, auth, NOW)).toMatchObject({ ok: false, status: 401 });
  });

  it("closes every authenticated route when nothing is configured", () => {
    // An unset secret must fail shut, not open.
    for (const cfg of [{}, { tokens: new Map<string, string>() }]) {
      const verdict = authorize(`Bearer ${TOKEN}`, cfg, NOW);
      expect(verdict).toMatchObject({ ok: false, status: 503 });
      expect(verdict.ok === false && verdict.message).toMatch(/no credentials are configured/);
    }
  });

  it("accepts a console session, and marks it as one", () => {
    const session = { secret: "s".repeat(32), ttlSecs: 3600 };
    const cfg = authConfig([{ grant: { slug: "acme" }, token: TOKEN }], session);
    const { token } = issueSession(session, "0x1111111111111111111111111111111111111111", "acme", NOW);
    const verdict = authorize(`Bearer ${token}`, cfg, NOW);
    expect(verdict).toEqual({
      ok: true,
      principal: { slug: "acme", kind: "console", operator: "0x1111111111111111111111111111111111111111" },
    });
  });

  it("opens the door for console sessions alone, with no grant tokens at all", () => {
    const session = { secret: "s".repeat(32), ttlSecs: 3600 };
    const cfg: AuthConfig = { tokens: new Map(), session };
    const { token } = issueSession(session, "0x1111111111111111111111111111111111111111", "acme", NOW);
    expect(authorize(`Bearer ${token}`, cfg, NOW).ok).toBe(true);
    expect(authorize(`Bearer ${TOKEN}`, cfg, NOW)).toMatchObject({ ok: false, status: 401 });
  });

  it("names a token too short to be random", () => {
    const said: string[] = [];
    warnWeakTokens([{ grant: { slug: "acme" }, token: "short" }], (s) => said.push(s));
    expect(said.join(" ")).toMatch(/acme.*5 characters/);
    said.length = 0;
    warnWeakTokens([{ grant: { slug: "acme" }, token: TOKEN }], (s) => said.push(s));
    expect(said).toEqual([]);
  });
});

describe("grants", () => {
  const one = (over: Record<string, unknown> = {}) =>
    JSON.stringify([{ slug: "acme", chainId: 4663, token: TOKEN, ...over }]);

  it("reads a grant and defaults signing off", () => {
    const [g] = parseGrants(one());
    expect(g!.grant.slug).toBe("acme");
    expect(g!.grant.chainId).toBe(4663);
    // A grant written without thinking about signing must not sign.
    expect(g!.grant.maySign).toBe(false);
    expect(g!.grant.maxSpendWei).toBeUndefined();
  });

  it("takes caps, and only as decimal strings", () => {
    const [g] = parseGrants(one({ maySign: true, maxLaunches: 20, maxSpendWei: "500000000000000000" }));
    expect(g!.grant.maySign).toBe(true);
    expect(g!.grant.maxLaunches).toBe(20);
    expect(g!.grant.maxSpendWei).toBe(500_000_000_000_000_000n);
    expect(() => parseGrants(one({ maxSpendWei: 5e17 }))).toThrow(GrantError);
  });

  it("refuses two grants that share a token", () => {
    const text = JSON.stringify([
      { slug: "acme", chainId: 4663, token: TOKEN },
      { slug: "zenith", chainId: 4663, token: TOKEN },
    ]);
    expect(() => parseGrants(text)).toThrow(/already used by another grant/);
  });

  it("refuses a duplicate slug, a short token, a bad slug and a missing chain", () => {
    expect(() =>
      parseGrants(
        JSON.stringify([
          { slug: "acme", chainId: 4663, token: TOKEN },
          { slug: "acme", chainId: 4663, token: OTHER },
        ]),
      ),
    ).toThrow(/appears twice/);
    expect(() => parseGrants(one({ token: "tiny" }))).toThrow(/at least 24/);
    expect(() => parseGrants(one({ slug: "Acme" }))).toThrow(/lowercase/);
    expect(() => parseGrants(JSON.stringify([{ slug: "acme", token: TOKEN }]))).toThrow(/set chainId/);
    expect(() => parseGrants("{}")).toThrow(/array of grants/);
    expect(() => parseGrants("[]")).toThrow(/lists no tenants/);
    expect(() => parseGrants("not json")).toThrow(/not JSON/);
  });

  it("takes an operator list, lowercased, and refuses anything that is not an address", () => {
    const [g] = parseGrants(one({ operators: ["0xAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaaAAaa"] }));
    expect(g!.grant.operators).toEqual(["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]);
    // Dropping a malformed one silently would leave an operator who cannot
    // sign in and no reason given.
    expect(() => parseGrants(one({ operators: ["not-an-address"] }))).toThrow(/expected a 0x address/);
    expect(() => parseGrants(one({ operators: "0x1" }))).toThrow(/array of addresses/);
    const dup = "0x1111111111111111111111111111111111111111";
    expect(() => parseGrants(one({ operators: [dup, dup.toUpperCase().replace("0X", "0x")] }))).toThrow(/listed twice/);
  });

  it("has no console operators unless the grant names them", () => {
    expect(parseGrants(one())[0]!.grant.operators).toBeUndefined();
  });

  it("accepts the { tenants: [...] } form", () => {
    expect(parseGrants(JSON.stringify({ tenants: [{ slug: "acme", chainId: 4663, token: TOKEN }] }))).toHaveLength(1);
  });
});

describe("a grant narrows the server, never widens it", () => {
  it("keeps a dry-run server dry even for a signing grant", () => {
    const dir = tmp();
    const rt = tenantRuntime(
      { slug: "acme", chainId: 4663, deployment: deploymentFile(dir), maySign: true },
      runCfg({ outDir: dir }),
    );
    expect(rt.run.dryRun).toBe(true);
    expect(serverCanSign(rt.run)).toBe(false);
  });

  it("turns signing off for a grant that may not sign, on a server that can", () => {
    const dir = tmp();
    const server = runCfg({ dryRun: false, privateKey: KEY, outDir: dir });
    const may = tenantRuntime({ slug: "acme", chainId: 4663, deployment: deploymentFile(dir), maySign: true }, server);
    const mayNot = tenantRuntime(
      { slug: "acme", chainId: 4663, deployment: deploymentFile(dir), maySign: false },
      server,
    );
    expect(serverCanSign(may.run)).toBe(true);
    expect(serverCanSign(mayNot.run)).toBe(false);
  });

  it("gives each tenant its own run directory, so one cannot read another's", () => {
    const dir = tmp();
    const rt = tenantRuntime({ slug: "acme", chainId: 4663, deployment: deploymentFile(dir), maySign: false }, runCfg());
    expect(rt.run.outDir).toBe(join("runs", "acme"));
  });

  it("refuses a grant whose deployment is for another chain", () => {
    const dir = tmp();
    expect(() =>
      tenantRuntime({ slug: "acme", chainId: 46630, deployment: deploymentFile(dir, 4663), maySign: false }, runCfg()),
    ).toThrow(/is for chain 4663/);
  });
});

describe("revoking a console operator", () => {
  const rt = (operators: string[]): TenantRuntime =>
    ({ grant: { slug: "acme", chainId: 4663, maySign: true, operators } }) as TenantRuntime;
  const A = "0x1111111111111111111111111111111111111111";
  const B = "0x2222222222222222222222222222222222222222";

  it("replaces the list without a restart, and says what changed", () => {
    const t = rt([A, B]);
    const changed = reloadOperators(
      () => [{ grant: { slug: "acme", chainId: 4663, maySign: true, operators: [A] }, token: "t" }],
      [t],
    );
    expect(t.grant.operators).toEqual([A]);
    expect(changed).toEqual([`acme: revoked ${B}`]);
  });

  it("names a swap, which counting would report as no change at all", () => {
    const t = rt([A]);
    const changed = reloadOperators(
      () => [{ grant: { slug: "acme", chainId: 4663, maySign: true, operators: [B] }, token: "t" }],
      [t],
    );
    expect(t.grant.operators).toEqual([B]);
    expect(changed).toEqual([`acme: revoked ${A}; added ${B}`]);
  });

  it("says nothing when nothing changed", () => {
    const t = rt([A]);
    expect(
      reloadOperators(
        () => [{ grant: { slug: "acme", chainId: 4663, maySign: true, operators: [A] }, token: "t" }],
        [t],
      ),
    ).toEqual([]);
  });

  it("changes nothing but the operators, so a running loop keeps its terms", () => {
    const t = rt([A]);
    t.grant.maxLaunches = 5;
    t.grant.maxSpendWei = 100n;
    reloadOperators(
      () => [
        {
          grant: { slug: "acme", chainId: 4663, maySign: false, operators: [B], maxLaunches: 999, maxSpendWei: 0n },
          token: "t",
        },
      ],
      [t],
    );
    expect(t.grant.operators).toEqual([B]);
    // A loop that started under these must not find them rewritten.
    expect(t.grant.maxLaunches).toBe(5);
    expect(t.grant.maxSpendWei).toBe(100n);
    expect(t.grant.maySign).toBe(true);
  });

  it("leaves a tenant the reload no longer mentions alone", () => {
    const t = rt([A]);
    expect(reloadOperators(() => [], [t])).toEqual([]);
    expect(t.grant.operators).toEqual([A]);
  });
});

describe("the budget a grant carries", () => {
  const ledger = () => join(tmp(), "budget.json");

  it("is unlimited only when no cap is set", () => {
    const b = new Budget(ledger());
    const r = b.reserve(10n ** 20n, 1000);
    expect(r.ok).toBe(true);
    expect(b.view().spendLeftWei).toBeUndefined();
  });

  it("refuses a launch past the launch cap", () => {
    const b = new Budget(ledger(), 2, undefined);
    expect(b.reserve(0n).ok).toBe(true);
    expect(b.reserve(0n).ok).toBe(true);
    const third = b.reserve(0n);
    expect(third.ok).toBe(false);
    if (!third.ok) expect(third.reason).toMatch(/allows 2 launches/);
    expect(b.view().launchesLeft).toBe(0);
  });

  it("refuses a buy past the spend cap", () => {
    const b = new Budget(ledger(), undefined, 1_000n);
    expect(b.reserve(600n).ok).toBe(true);
    const second = b.reserve(600n);
    expect(second.ok).toBe(false);
    if (!second.ok) expect(second.reason).toMatch(/1000 wei of opening buys/);
    expect(b.view().spendLeftWei).toBe("400");
  });

  it("refunds what a run did not spend", () => {
    const b = new Budget(ledger(), 10, 1_000n);
    const r = b.reserve(600n);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    // The factory clamped the opening buy to its launch-window cap.
    b.settle(r.handle, 250n, 1);
    expect(b.view()).toMatchObject({ spentWei: "250", launches: 1, spendLeftWei: "750" });
  });

  it("gives a launch slot back when nothing was signed", () => {
    const b = new Budget(ledger(), 1, 1_000n);
    const r = b.reserve(600n);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    b.release(r.handle);
    expect(b.view()).toMatchObject({ spentWei: "0", launches: 0 });
    // The slot is usable again, which it would not be if a design failure
    // burned it.
    expect(b.reserve(600n).ok).toBe(true);
  });

  it("cannot be refunded twice", () => {
    const b = new Budget(ledger(), 10, 1_000n);
    const r = b.reserve(600n);
    if (!r.ok) return;
    b.release(r.handle);
    b.release(r.handle);
    b.settle(r.handle, 600n, 1);
    expect(b.view()).toMatchObject({ spentWei: "0", launches: 0 });
  });

  it("settles a loop against what it really launched", () => {
    const b = new Budget(ledger(), 100, 10n ** 18n);
    // A loop reserves the worst case: 10 launches at 0.01 ETH.
    const r = b.reserve(10n ** 16n * 10n, 10);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(b.view().launches).toBe(10);
    // It managed three, at a cent each.
    b.settle(r.handle, 3n * 10n ** 15n, 3, 10);
    expect(b.view()).toMatchObject({ launches: 3, spentWei: "3000000000000000" });
  });

  it("does not hand a restarted process a fresh allowance", () => {
    const path = ledger();
    const first = new Budget(path, 10, 1_000n);
    const r = first.reserve(400n);
    if (!r.ok) return;
    first.settle(r.handle, 400n, 1);
    const second = new Budget(path, 10, 1_000n);
    expect(second.view()).toMatchObject({ spentWei: "400", launches: 1, spendLeftWei: "600" });
  });

  it("refuses to spend when its ledger cannot be read", () => {
    const path = ledger();
    writeFileSync(path, "{ not json");
    const b = new Budget(path, 10, 1_000n);
    const r = b.reserve(1n);
    expect(r.ok).toBe(false);
    // A lost ledger must not read as a reset allowance.
    if (!r.ok) expect(r.reason).toMatch(/cannot be read/);
    expect(b.view().broken).toBeDefined();
    // And it must not advertise an allowance it will not honour.
    expect(b.view()).toMatchObject({ launchesLeft: 0, spendLeftWei: "0" });
  });
});

describe("what a finished run really cost", () => {
  it("charges nothing for a dry run or a run that never published", () => {
    expect(spentByRun({})).toEqual({ wei: 0n, launches: 0 });
    expect(spentByRun({ publish: { dryRun: true, firstBuyWei: 10n } })).toEqual({ wei: 0n, launches: 0 });
  });

  it("charges a live launch its opening buy", () => {
    expect(spentByRun({ publish: { dryRun: false, firstBuyWei: 10n } })).toEqual({ wei: 10n, launches: 1 });
  });

  it("sums a loop over the launches that signed", () => {
    expect(
      spentByLoop({
        results: [
          { publish: { dryRun: false, firstBuyWei: 5n } },
          { publish: { dryRun: true, firstBuyWei: 99n } },
          {},
          { publish: { dryRun: false, firstBuyWei: 7n } },
        ],
      }),
    ).toEqual({ wei: 12n, launches: 2 });
  });

  it("reserves against the gene pool's ceiling, not the seed's opening buy", () => {
    // firstBuyWei is a gene, so a later generation can bid higher than the seed.
    const seed = baseSpec({ profile: "crypto", topic: "t", tenant: "acme", chainId: 4663, firstBuyWei: 0n });
    expect(worstCasePerLaunch(seed)).toBe(10n ** 16n);
    const rich = baseSpec({ profile: "crypto", topic: "t", tenant: "acme", chainId: 4663, firstBuyWei: 10n ** 18n });
    expect(worstCasePerLaunch(rich)).toBe(10n ** 18n);
  });

  it("treats an unreadable nonce as having broadcast", () => {
    expect(broadcast(4, 4)).toBe(false);
    expect(broadcast(4, 5)).toBe(true);
    // Guessing the other way costs ETH, so not knowing counts as yes.
    expect(broadcast(undefined, 5)).toBe(true);
    expect(broadcast(4, undefined)).toBe(true);
  });
});

describe("what has to line up before anything signs", () => {
  it("needs the server, the grant and the request", () => {
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
