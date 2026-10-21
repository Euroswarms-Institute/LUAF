/** The service. What used to be a CLI, as the website's backend.
 *
 *  Configuration is resolved once at startup and a bad setting stops the
 *  process. A service that boots with a broken deployment and reports it as a
 *  500 on the first request has turned an operator's mistake into an outage. */

import { createServer } from "node:http";

import {
  llmConfig,
  loadLauncherDeployment,
  runConfig,
  runOnce,
  type PipelineResult,
} from "@lucreocracy/core";

import { authConfigFromEnv, warnWeakToken } from "./auth.js";
import { Jobs } from "./jobs.js";
import { Loop } from "./loop.js";
import { specFromRequest, type RunRequest } from "./request.js";
import { handle, serverCanSign, type Deps } from "./routes.js";

function envInt(name: string, fallback: number): number {
  const v = process.env[name]?.trim();
  if (!v) return fallback;
  if (!/^\d+$/.test(v)) throw new Error(`${name}=${v}: expected an integer`);
  return Number(v);
}

/** ETH/USD with 8 decimals. Supplied, not fetched: it sizes the curve's start
 *  price and the factory rejects anything outside its deployed band, so a
 *  wrong automatic value would open curves at the wrong price. */
function ethUsd8(): bigint {
  const v = process.env.LUCRE_ETH_USD?.trim();
  if (!v) throw new Error("set LUCRE_ETH_USD (dollars, e.g. 2500)");
  if (!/^\d+(\.\d{1,8})?$/.test(v)) throw new Error(`LUCRE_ETH_USD=${v}: expected a dollar amount`);
  const [whole = "0", frac = ""] = v.split(".");
  return BigInt(whole) * 10n ** 8n + BigInt(frac.padEnd(8, "0"));
}

function parseEth(name: string, fallback: string): bigint {
  const v = process.env[name]?.trim() || fallback;
  if (!/^\d+(\.\d{1,18})?$/.test(v)) throw new Error(`${name}=${v}: expected an ETH amount`);
  const [whole = "0", frac = ""] = v.split(".");
  return BigInt(whole) * 10n ** 18n + BigInt(frac.padEnd(18, "0"));
}

async function main(): Promise<void> {
  const auth = authConfigFromEnv();
  const dep = loadLauncherDeployment();
  const llm = llmConfig();
  const run = runConfig();
  const usd8 = ethUsd8();
  const defaultProfile = process.env.LUCRE_PROFILE?.trim() || "crypto";
  const port = envInt("PORT", 8080);

  const jobs = new Jobs();
  const loop = new Loop({
    dep,
    llm,
    run,
    ethUsd8: usd8,
    defaultProfile,
    targetEthWei: parseEth("LUCRE_TARGET_ETH", "1"),
    maxLaunches: envInt("LUCRE_MAX_LAUNCHES", 10),
    populationPath: process.env.LUCRE_POPULATION?.trim() || "runs/population.json",
    aggregatorUrl: process.env.LUCRE_AGGREGATOR_URL?.trim() || undefined,
    log: (line) => console.log(line),
  });

  const runPipeline = async (args: {
    request: RunRequest;
    live: boolean;
    log: (line: string) => void;
    signal: AbortSignal;
  }): Promise<PipelineResult> => {
    const spec = specFromRequest(args.request, { tenant: dep.tenant, chainId: dep.chainId, defaultProfile });
    if (!spec.ok) throw new Error(spec.error);
    return runOnce({
      spec: spec.value,
      dep,
      llm,
      // A request that did not ask to sign runs against a dry-run config, so
      // the pipeline itself cannot sign even if something below it tried.
      run: args.live ? run : { ...run, dryRun: true },
      ethUsd8: args.request.designOnly ? 0n : usd8,
      designOnly: args.request.designOnly,
      log: args.log,
      signal: args.signal,
    });
  };

  const deps: Deps = {
    auth,
    jobs,
    dep,
    llm,
    run,
    ethUsd8: usd8,
    runPipeline,
    loop: {
      state: () => loop.current(),
      start: (request) => loop.start(request),
      stop: () => loop.stop(),
    },
  };

  const server = createServer((req, res) => {
    void handle(req, res, deps).catch(() => {
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
      res.end('{"error":"internal"}');
    });
  });

  server.listen(port, () => {
    console.log(`lucreocracy service on :${port}`);
    console.log(`  tenant ${dep.tenant} on chain ${dep.chainId}, model ${llm.model}`);
    console.log(
      serverCanSign(run)
        ? "  CAN SIGN: a request asking for live will launch for real"
        : "  dry run only: no request can make this instance sign",
    );
    warnWeakToken(auth, (s) => console.warn(`  ${s}`));
  });

  const shutdown = () => {
    loop.stop();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 5_000).unref();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((e) => {
  console.error(e instanceof Error ? e.message : e);
  process.exit(1);
});
