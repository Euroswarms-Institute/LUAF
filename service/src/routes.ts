/** The HTTP surface the website's backend calls.
 *
 *  This replaces the CLI one command at a time, with one deliberate
 *  difference. The CLI's real safeguard against an accidental live launch was
 *  not `--no-dry-run`; it was that you had to be on the machine with a shell.
 *  An HTTP endpoint hands that away to anyone holding a token, so signing is
 *  rebuilt to need **two independent keys turning**:
 *
 *    1. the server must have been started to sign at all (LUCRE_DRY_RUN=0 with
 *       an operator key), and
 *    2. the request must explicitly ask for it (`"live": true`).
 *
 *  Either alone is a dry run. A request cannot talk a read-only server into
 *  signing, and a signing server does not sign by default — so a website bug
 *  that forgets a field costs nothing. */

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
  type LauncherDeployment,
  type LlmConfig,
  type PipelineResult,
  type RunConfig,
} from "@lucreocracy/core";

import { authorize, type AuthConfig } from "./auth.js";
import type { Jobs } from "./jobs.js";
import { parseRunRequest, type RunRequest } from "./request.js";

export interface Deps {
  auth: AuthConfig;
  jobs: Jobs;
  /** Resolved once at startup; a config error should stop the process, not
   *  surface as a 500 on the first request. */
  dep: LauncherDeployment;
  llm: LlmConfig;
  run: RunConfig;
  /** ETH/USD, 8dp. Supplied rather than fetched: it sizes the curve's start
   *  price and the factory checks it against its band. */
  ethUsd8: bigint;
  /** Injected so routes can be tested without an LLM or a chain. */
  runPipeline: (args: {
    request: RunRequest;
    live: boolean;
    log: (line: string) => void;
    signal: AbortSignal;
  }) => Promise<PipelineResult>;
  loop: {
    state(): { running: boolean; startedAt?: number; launches: number; stopped?: string; note?: string };
    start(request: RunRequest): { ok: boolean; message: string };
    stop(): { ok: boolean; message: string };
  };
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

/** Whether this server is permitted to sign at all. The first of the two keys. */
export function serverCanSign(run: RunConfig): boolean {
  return !run.dryRun && !!run.privateKey;
}

/** Resolve the two-key rule for one request. */
export function resolveLive(request: RunRequest, run: RunConfig): { live: boolean; refusal?: string } {
  if (!request.live) return { live: false };
  if (!serverCanSign(run)) {
    return {
      live: false,
      refusal:
        "this server is not configured to sign: it was started in dry-run mode, or without an operator key. " +
        "A request cannot grant itself signing authority.",
    };
  }
  return { live: true };
}

export async function handle(req: IncomingMessage, res: ServerResponse, deps: Deps): Promise<void> {
  const url = new URL(req.url ?? "/", "http://service.invalid");
  const path = url.pathname.replace(/\/+$/, "") || "/";
  const method = req.method ?? "GET";

  // Liveness is the one unauthenticated route, and it says nothing about
  // configuration: an open probe should not describe the machine.
  if (method === "GET" && path === "/health") {
    json(res, ok({ ok: true }));
    return;
  }

  const auth = authorize(req.headers.authorization, deps.auth);
  if (!auth.ok) {
    json(res, bad(auth.status, auth.message));
    return;
  }

  try {
    json(res, await route(method, path, url, req, deps));
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
): Promise<Reply> {
  // --- diagnostics -------------------------------------------------------
  if (method === "GET" && path === "/ready") {
    const profiles = listProfiles();
    return ok({
      ok: true,
      tenant: deps.dep.tenant,
      chainId: deps.dep.chainId,
      launcher: deps.dep.contracts.launcher,
      factory: deps.dep.contracts.factory,
      model: deps.llm.model,
      profiles: profiles.length,
      // The single most important fact about this deployment.
      canSign: serverCanSign(deps.run),
      loop: deps.loop.state(),
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

  // --- runs --------------------------------------------------------------
  if (method === "POST" && path === "/runs") {
    const parsed = parseRunRequest(await readBody(req));
    if (!parsed.ok) return bad(400, parsed.error);
    const { live, refusal } = resolveLive(parsed.value, deps.run);
    if (refusal) return bad(403, refusal);
    const request = parsed.value;
    const job = deps.jobs.start({
      live,
      run: ({ log, signal }) => deps.runPipeline({ request, live, log, signal }),
    });
    return { status: 202, body: { id: job.id, state: job.state, live: job.live } };
  }

  if (method === "GET" && path === "/runs") {
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get("limit") ?? 20) || 20));
    return ok({ runs: deps.jobs.list(limit) });
  }

  const runMatch = /^\/runs\/([0-9a-fA-F-]{36})$/.exec(path);
  if (method === "GET" && runMatch) {
    const job = deps.jobs.get(runMatch[1]!);
    if (!job) return bad(404, "no such run");
    return ok(deps.jobs.view(job));
  }

  const cancelMatch = /^\/runs\/([0-9a-fA-F-]{36})\/cancel$/.exec(path);
  if (method === "POST" && cancelMatch) {
    const id = cancelMatch[1]!;
    if (!deps.jobs.get(id)) return bad(404, "no such run");
    return ok({ cancelled: deps.jobs.cancel(id) });
  }

  // --- the loop ----------------------------------------------------------
  if (method === "GET" && path === "/loop") return ok(deps.loop.state());

  if (method === "POST" && path === "/loop/start") {
    const parsed = parseRunRequest(await readBody(req));
    if (!parsed.ok) return bad(400, parsed.error);
    const { refusal } = resolveLive(parsed.value, deps.run);
    if (refusal) return bad(403, refusal);
    const started = deps.loop.start(parsed.value);
    return started.ok ? ok({ ...deps.loop.state(), message: started.message }) : bad(409, started.message);
  }

  if (method === "POST" && path === "/loop/stop") {
    const stopped = deps.loop.stop();
    return stopped.ok ? ok({ ...deps.loop.state(), message: stopped.message }) : bad(409, stopped.message);
  }

  // --- what it earned ----------------------------------------------------
  if (method === "GET" && path === "/income") {
    const rows = await collectIncome(deps.dep, deps.run.rpcUrl);
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
      splitter: deps.dep.contracts.creatorSplitter,
      launchChainId: deps.dep.chainId,
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
