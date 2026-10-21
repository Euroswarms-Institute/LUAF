/** The HTTP surface the website's backend calls.
 *
 *  This replaces the CLI, with two deliberate differences.
 *
 *  First, signing. The CLI's real safeguard against an accidental live launch
 *  was not `--no-dry-run`; it was that you had to be on the machine with a
 *  shell. An HTTP endpoint hands that away to anyone holding a token, so
 *  signing is rebuilt to need **three things to line up**: the server was
 *  started to sign at all (LUCRE_DRY_RUN=0 with an operator key), the tenant's
 *  grant permits it, and the request explicitly asks for it. Any one missing
 *  is a dry run. A request cannot talk a read-only server into signing, and a
 *  signing server does not sign by default — so a website bug that forgets a
 *  field costs nothing.
 *
 *  Second, tenancy. The tenant is resolved from the credential, never from the
 *  request, and every route below is scoped to that tenant: its deployment,
 *  its chain, its jobs, its loop, its budget. A token is the whole of a
 *  caller's authority, and a caller cannot edit their own token. */

import type { IncomingMessage, ServerResponse } from "node:http";

import {
  bySpec,
  chainIdForNetwork,
  collectEndpointIncome,
  collectIncome,
  concentration,
  listProfiles,
  loadProfile,
  resolveEndpointConfig,
  specId as specIdOf,
  type LaunchSpec,
  type LlmConfig,
  type PipelineResult,
  type RunConfig,
} from "@lucreocracy/core";

import { authorize, type AuthConfig, type Principal } from "./auth.js";
import type { Jobs } from "./jobs.js";
import { parseRunRequest, specFromRequest, type RunRequest } from "./request.js";
import { signIn, type Nonces, type SessionConfig } from "./siwe.js";
import { broadcast, spentByRun, worstCasePerLaunch } from "./spend.js";
import type { Reservation, TenantGrant, TenantRuntime } from "./tenants.js";

/** Everything one tenant needs, kept apart from every other tenant's. */
export interface TenantService {
  runtime: TenantRuntime;
  jobs: Jobs;
  /** Ceiling the loop was configured with, used to size its reservation. */
  loopMaxLaunches: number;
  loop: {
    state(): { running: boolean; startedAt?: number; launches: number; stopped?: string; note?: string };
    start(args: { spec: LaunchSpec; reservation?: Reservation; reservedLaunches: number }): {
      ok: boolean;
      message: string;
    };
    stop(): { ok: boolean; message: string };
  };
  /** Injected so routes can be tested without an LLM or a chain. */
  runPipeline: (args: {
    spec: LaunchSpec;
    live: boolean;
    designOnly: boolean;
    log: (line: string) => void;
    signal: AbortSignal;
  }) => Promise<PipelineResult>;
  /** The operator's transaction count, used to tell a run that broadcast from
   *  one that failed before it did. Undefined when it cannot be read. */
  operatorNonce: () => Promise<number | undefined>;
}

export interface Deps {
  auth: AuthConfig;
  tenants: Map<string, TenantService>;
  /** The grants themselves, for console sign-in: a wallet proves itself
   *  against the operator list of the launchpad it names. */
  grants: Map<string, TenantGrant>;
  session: SessionConfig;
  nonces: Nonces;
  /** The console page, read once at startup. Absent means no page is served. */
  consolePage?: string;
  llm: LlmConfig;
  /** The profile a request that names none gets. */
  defaultProfile: string;
  /** ETH/USD, 8dp. Supplied rather than fetched: it sizes the curve's start
   *  price and the factory checks it against its band. */
  ethUsd8: bigint;
}

interface Reply {
  status: number;
  body: unknown;
}

const ok = (body: unknown): Reply => ({ status: 200, body });
const bad = (status: number, message: string): Reply => ({ status, body: { error: message } });

function json(res: ServerResponse, reply: Reply): void {
  const text = JSON.stringify(reply.body);
  res.writeHead(reply.status, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(text),
    // Nothing here is for a browser to cache or a crawler to read.
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  res.end(text);
}

const MAX_BODY_BYTES = 64 * 1024;

export function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk: Buffer) => {
      data += chunk.toString();
      if (data.length > MAX_BODY_BYTES) {
        reject(new Error("request body too large"));
        req.destroy();
      }
    });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

/** Routes a console session may not reach.
 *
 *  Short, and deliberately so: the real narrowing on a console session is that
 *  it lasts hours rather than forever and is bound to a wallet we allowlisted,
 *  not a long list of forbidden paths. Pretending otherwise would be theatre.
 *  What is here is here because a browser has no use for it — endpoint income
 *  is a read of an unrelated payment chain, configured by server environment. */
const NOT_FOR_CONSOLE = new Set(["/endpoint-income"]);

/** Whether this server, as narrowed for this tenant, may sign at all. */
export function serverCanSign(run: RunConfig): boolean {
  return !run.dryRun && !!run.privateKey;
}

/** Resolve for one request whether it signs. */
export function resolveLive(request: RunRequest, run: RunConfig): { live: boolean; refusal?: string } {
  if (!request.live) return { live: false };
  if (!serverCanSign(run)) {
    return {
      live: false,
      refusal:
        "this server is not configured to sign for this tenant: it was started in dry-run mode, without an " +
        "operator key, or the tenant's grant does not permit signing. A request cannot grant itself signing authority.",
    };
  }
  return { live: true };
}

const nowSecs = () => Math.floor(Date.now() / 1000);

export async function handle(req: IncomingMessage, res: ServerResponse, deps: Deps): Promise<void> {
  const url = new URL(req.url ?? "/", "http://service.invalid");
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = req.method ?? "GET";

  // Liveness is unauthenticated, and it says nothing about configuration: an
  // open probe should not describe the machine.
  if (method === "GET" && path === "/health") {
    json(res, ok({ ok: true }));
    return;
  }

  // The console page itself holds no secrets — it is the thing that asks for a
  // signature — so it is served before any credential is looked for.
  if (method === "GET" && path === "/console") {
    if (!deps.consolePage) {
      json(res, bad(404, "no console page is installed"));
      return;
    }
    const body = Buffer.from(deps.consolePage);
    res.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "content-length": body.byteLength,
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      // The page talks to this origin only and embeds nothing beyond its own
      // inline icon.
      "content-security-policy":
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
    });
    res.end(body);
    return;
  }

  // Sign-in is how a credential is obtained, so it cannot require one.
  if (method === "POST" && path === "/console/auth") {
    let body: unknown;
    try {
      const text = await readBody(req);
      body = text.trim() ? JSON.parse(text) : {};
    } catch (e) {
      json(res, bad(400, `body is not JSON: ${(e as Error).message}`));
      return;
    }
    const result = await signIn({
      body,
      grants: deps.grants,
      session: deps.session,
      nonces: deps.nonces,
      nowSecs: nowSecs(),
    });
    json(res, result.ok ? ok(result) : bad(result.status, result.error));
    return;
  }

  const auth = authorize(req.headers.authorization, deps.auth, nowSecs());
  if (!auth.ok) {
    json(res, bad(auth.status, auth.message));
    return;
  }
  const { principal } = auth;
  const tenant = deps.tenants.get(principal.slug);
  if (!tenant) {
    // A valid credential with no tenant behind it is our misconfiguration, not
    // the caller's mistake, and saying so plainly is what makes it fixable.
    json(res, bad(503, `the grant for ${principal.slug} did not load, so this credential cannot be served`));
    return;
  }
  if (principal.kind === "console") {
    // Rechecked on every call rather than only at sign-in, so revoking a
    // wallet does not wait for its session to expire: remove it from the grant
    // file and send the service SIGHUP.
    if (!(tenant.runtime.grant.operators ?? []).includes(principal.operator ?? "")) {
      json(res, bad(401, "that wallet is no longer an operator of this launchpad"));
      return;
    }
    if (NOT_FOR_CONSOLE.has(path)) {
      json(res, bad(403, `${path} is not reachable from a console session`));
      return;
    }
  }

  try {
    json(res, await route(method, path, url, req, deps, tenant, principal));
  } catch (e) {
    json(res, bad(500, e instanceof Error ? e.message : String(e)));
  }
}

async function route(
  method: string,
  path: string,
  url: URL,
  req: IncomingMessage,
  deps: Deps,
  t: TenantService,
  principal: Principal,
): Promise<Reply> {
  const { runtime } = t;
  const { dep, run, budget, grant } = runtime;

  // --- diagnostics -------------------------------------------------------
  if (method === "GET" && path === "/ready") {
    return ok({
      ok: true,
      tenant: dep.tenant,
      chainId: dep.chainId,
      launcher: dep.contracts.launcher,
      factory: dep.contracts.factory,
      model: deps.llm.model,
      profiles: listProfiles().length,
      // The two most important facts about this grant.
      canSign: serverCanSign(run),
      maySign: grant.maySign,
      budget: budget.view(),
      loop: t.loop.state(),
      as: { kind: principal.kind, ...(principal.operator ? { operator: principal.operator } : {}) },
    });
  }

  if (method === "GET" && path === "/profiles") {
    return ok({
      profiles: listProfiles().map((id) => {
        const p = loadProfile(id);
        return { id, displayName: p.displayName };
      }),
    });
  }

  if (method === "GET" && path === "/budget") return ok(budget.view());

  // --- runs --------------------------------------------------------------
  if (method === "POST" && path === "/runs") {
    const parsed = parseRunRequest(await readBody(req));
    if (!parsed.ok) return bad(400, parsed.error);
    const { live, refusal } = resolveLive(parsed.value, run);
    if (refusal) return bad(403, refusal);

    // Build the spec here rather than inside the job: a spec the factory would
    // reject is the caller's mistake, and belongs in the reply, not in a log.
    const spec = specFromRequest(parsed.value, {
      tenant: dep.tenant,
      chainId: dep.chainId,
      defaultProfile: deps.defaultProfile,
    });
    if (!spec.ok) return bad(400, spec.error);
    const designOnly = parsed.value.designOnly;

    // Debit the grant before the work starts, so two requests in flight cannot
    // both be checked against the same balance.
    let handle: Reservation | undefined;
    if (live && !designOnly) {
      const reserved = budget.reserve(spec.value.firstBuyWei);
      if (!reserved.ok) return bad(402, reserved.reason);
      handle = reserved.handle;
    }

    const nonceBefore = handle ? await t.operatorNonce() : undefined;
    const job = t.jobs.start({
      live: live && !designOnly,
      run: async ({ log, signal }) => {
        try {
          const result = await t.runPipeline({ spec: spec.value, live, designOnly, log, signal });
          if (handle) {
            const real = spentByRun(result);
            budget.settle(handle, real.wei, real.launches);
          }
          return result;
        } catch (e) {
          if (handle) {
            // Ask the chain whether anything was sent before refunding.
            const after = await t.operatorNonce();
            if (broadcast(nonceBefore, after)) budget.settle(handle, handle.wei, 1);
            else budget.release(handle);
          }
          throw e;
        }
      },
    });
    return { status: 202, body: { id: job.id, state: job.state, live: job.live, specId: specIdOf(spec.value) } };
  }

  if (method === "GET" && path === "/runs") {
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit") ?? 20) || 20));
    return ok({ runs: t.jobs.list(limit) });
  }

  const runMatch = /^\/runs\/([0-9a-fA-F-]{36})$/.exec(path);
  if (method === "GET" && runMatch) {
    const job = t.jobs.get(runMatch[1]!);
    if (!job) return bad(404, "no such run");
    return ok(t.jobs.view(job));
  }

  const cancelMatch = /^\/runs\/([0-9a-fA-F-]{36})\/cancel$/.exec(path);
  if (method === "POST" && cancelMatch) {
    const id = cancelMatch[1]!;
    if (!t.jobs.get(id)) return bad(404, "no such run");
    return ok({ cancelled: t.jobs.cancel(id) });
  }

  // --- the loop ----------------------------------------------------------
  if (method === "GET" && path === "/loop") return ok(t.loop.state());

  if (method === "POST" && path === "/loop/start") {
    const parsed = parseRunRequest(await readBody(req));
    if (!parsed.ok) return bad(400, parsed.error);
    const { live, refusal } = resolveLive(parsed.value, run);
    if (refusal) return bad(403, refusal);
    const spec = specFromRequest(parsed.value, {
      tenant: dep.tenant,
      chainId: dep.chainId,
      defaultProfile: deps.defaultProfile,
    });
    if (!spec.ok) return bad(400, spec.error);

    // A loop's later specs do not exist yet, so it reserves its worst case up
    // front: the gene pool's largest opening buy, times the launches it is
    // allowed. Whatever it does not spend is refunded when it stops.
    let handle: Reservation | undefined;
    const reservedLaunches = t.loopMaxLaunches;
    if (live) {
      const reserved = budget.reserve(worstCasePerLaunch(spec.value) * BigInt(reservedLaunches), reservedLaunches);
      if (!reserved.ok) return bad(402, reserved.reason);
      handle = reserved.handle;
    }
    const started = t.loop.start({ spec: spec.value, reservation: handle, reservedLaunches });
    if (!started.ok) {
      if (handle) budget.release(handle, reservedLaunches);
      return bad(409, started.message);
    }
    return ok({ ...t.loop.state(), message: started.message, budget: budget.view() });
  }

  if (method === "POST" && path === "/loop/stop") {
    const stopped = t.loop.stop();
    return stopped.ok ? ok({ ...t.loop.state(), message: stopped.message }) : bad(409, stopped.message);
  }

  // --- what it earned ----------------------------------------------------
  if (method === "GET" && path === "/income") {
    const rows = await collectIncome(dep, run.rpcUrl);
    let total = 0n;
    const specs = [...bySpec(rows).values()].map((s) => {
      total += s.totalEthWei;
      return {
        specId: s.specId,
        coins: s.tokens.length,
        graduated: s.graduated,
        totalEthWei: s.totalEthWei.toString(),
        meanTurnoverSecs: s.meanTurnoverSecs,
        turnoversComplete: s.turnoversComplete,
      };
    });
    return ok({
      // Every amount is a decimal string: a wei value does not survive JSON's
      // number type, and silently rounding money is not an option.
      totalEthWei: total.toString(),
      coins: rows.map((r) => ({
        token: r.token,
        specId: r.specId,
        graduated: r.graduated,
        turnoverSecs: r.turnoverSecs,
        turnoverComplete: r.turnoverComplete,
        totalEthWei: r.totalEthWei.toString(),
        swaps: r.swaps,
      })),
      specs,
    });
  }

  if (method === "GET" && path === "/endpoint-income") {
    const cfg = resolveEndpointConfig({
      splitter: dep.contracts.creatorSplitter,
      launchChainId: dep.chainId,
    });
    const chainId = cfg.chainId ?? chainIdForNetwork(cfg.network);
    if (chainId === null) return bad(400, `X402_NETWORK=${cfg.network} resolves to no known chain id`);
    const rpcUrl = process.env.X402_RPC_URL?.trim();
    if (!rpcUrl) return bad(400, `set X402_RPC_URL: payments settle on chain ${chainId}, not the launch chain`);
    const income = await collectEndpointIncome({
      rpcUrl,
      chainId,
      asset: cfg.asset as `0x${string}`,
      payTo: cfg.payTo as `0x${string}`,
    });
    const verdict = concentration(income);
    return ok({
      payTo: income.payTo,
      asset: income.asset,
      chainId: income.chainId,
      totalAtomic: income.totalAtomic.toString(),
      payments: income.payments,
      uniquePayers: income.uniquePayers,
      largestPayerAtomic: income.largestPayerAtomic.toString(),
      verdict,
      note:
        "the only income here that is not a transfer between traders; /income reports the fee side, " +
        "which is a share of value produced elsewhere",
    });
  }

  return bad(404, `no route for ${method} ${path}`);
}
