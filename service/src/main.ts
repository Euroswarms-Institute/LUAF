/** The service. What used to be a CLI, as the website's backend.
 *
 *  Configuration is resolved once at startup and a bad setting stops the
 *  process. A service that boots with a broken deployment and reports it as a
 *  500 on the first request has turned an operator's mistake into an outage.
 *
 *  One process serves every tenant whose grant is listed. There is no shared
 *  state between them: separate deployments, separate run directories,
 *  separate job histories, separate loops, separate budgets. The only thing
 *  they share is the operator key, which is exactly why each grant carries a
 *  spend ceiling. */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import {
  llmConfig,
  operatorNonce,
  runConfig,
  runOnce,
  type LaunchSpec,
  type PipelineResult,
  type RunConfig,
} from "@lucreocracy/core";

import { authConfig, warnWeakTokens } from "./auth.js";
import { Jobs } from "./jobs.js";
import { Loop } from "./loop.js";
import { handle, serverCanSign, type Deps, type TenantService } from "./routes.js";
import { GrantError, parseGrants, tenantRuntime, type TenantGrant } from "./tenants.js";

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

/** The grants this process serves.
 *
 *  A grant file is the multi-tenant form. Without one the service runs for a
 *  single tenant out of the environment, which is the operator serving
 *  themselves: that grant is unlimited, because the ceilings exist to bound
 *  somebody else's use of our key, not our own. */
function grantsFromEnv(): { grant: TenantGrant; token: string }[] {
  const file = process.env.LUCRE_TENANTS_FILE?.trim();
  if (file) {
    let text: string;
    try {
      text = readFileSync(file, "utf8");
    } catch (e) {
      throw new GrantError(`can't read LUCRE_TENANTS_FILE=${file}: ${(e as Error).message}`);
    }
    return parseGrants(text);
  }
  const token = process.env.LUCRE_API_TOKEN?.trim();
  const slug = process.env.LUCRE_TENANT?.trim();
  if (!token) throw new GrantError("set LUCRE_TENANTS_FILE, or LUCRE_API_TOKEN with LUCRE_TENANT");
  if (!slug) throw new GrantError("set LUCRE_TENANT (or use LUCRE_TENANTS_FILE for more than one tenant)");
  return [
    {
      token,
      grant: {
        slug,
        chainId: envInt("LUCRE_CHAIN_ID", 46630),
        maySign: true,
        ...(process.env.LUCRE_LAUNCHER_DEPLOYMENT?.trim()
          ? { deployment: process.env.LUCRE_LAUNCHER_DEPLOYMENT.trim() }
          : {}),
      },
    },
  ];
}

function buildTenant(
  grant: TenantGrant,
  serverRun: RunConfig,
  shared: { llm: ReturnType<typeof llmConfig>; usd8: bigint },
): TenantService {
  const runtime = tenantRuntime(grant, serverRun);
  const { dep, run, budget } = runtime;
  const log = (line: string) => console.log(`[${grant.slug}] ${line}`);

  const loop = new Loop({
    dep,
    llm: shared.llm,
    run,
    ethUsd8: shared.usd8,
    budget,
    targetEthWei: parseEth("LUCRE_TARGET_ETH", "1"),
    maxLaunches: envInt("LUCRE_MAX_LAUNCHES", 10),
    populationPath: join(run.outDir, "population.json"),
    aggregatorUrl: process.env.LUCRE_AGGREGATOR_URL?.trim() || undefined,
    log,
  });

  const runPipeline = async (args: {
    spec: LaunchSpec;
    live: boolean;
    designOnly: boolean;
    log: (line: string) => void;
    signal: AbortSignal;
  }): Promise<PipelineResult> =>
    runOnce({
      spec: args.spec,
      dep,
      llm: shared.llm,
      // A request that did not ask to sign runs against a dry-run config, so
      // the pipeline itself cannot sign even if something below it tried.
      run: args.live ? run : { ...run, dryRun: true },
      ethUsd8: args.designOnly ? 0n : shared.usd8,
      designOnly: args.designOnly,
      log: args.log,
      signal: args.signal,
    });

  return {
    runtime,
    jobs: new Jobs(),
    loopMaxLaunches: envInt("LUCRE_MAX_LAUNCHES", 10),
    loop: {
      state: () => loop.current(),
      start: (a) => loop.start(a),
      stop: () => loop.stop(),
    },
    runPipeline,
    operatorNonce: () => operatorNonce(dep, run.rpcUrl),
  };
}

async function main(): Promise<void> {
  const grants = grantsFromEnv();
  const llm = llmConfig();
  const run = runConfig();
  const usd8 = ethUsd8();
  const defaultProfile = process.env.LUCRE_PROFILE?.trim() || "crypto";
  const port = envInt("PORT", 8080);

  const tenants = new Map<string, TenantService>();
  for (const { grant } of grants) {
    tenants.set(grant.slug, buildTenant(grant, run, { llm, usd8 }));
  }

  const deps: Deps = { auth: authConfig(grants), tenants, llm, defaultProfile, ethUsd8: usd8 };

  const server = createServer((req, res) => {
    void handle(req, res, deps).catch(() => {
      if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
      res.end('{"error":"internal"}');
    });
  });

  server.listen(port, () => {
    console.log(`lucreocracy service on :${port}`);
    console.log(
      serverCanSign(run)
        ? "  CAN SIGN: a request from a signing grant will launch for real"
        : "  dry run only: no request can make this instance sign",
    );
    console.log(`  model ${llm.model}, default profile ${defaultProfile}`);
    for (const t of tenants.values()) {
      const b = t.runtime.budget.view();
      const signs = serverCanSign(t.runtime.run) ? "signs" : "dry run";
      const cap = b.maxSpendWei === undefined ? "no spend cap" : `${b.spendLeftWei} wei left of ${b.maxSpendWei}`;
      const count =
        b.maxLaunches === undefined ? "no launch cap" : `${b.launchesLeft} launches left of ${b.maxLaunches}`;
      console.log(`  ${t.runtime.dep.tenant} on chain ${t.runtime.dep.chainId}: ${signs}, ${count}, ${cap}`);
      if (b.broken) console.warn(`    ${b.broken}`);
    }
    warnWeakTokens(grants, (s) => console.warn(`  ${s}`));
  });

  const shutdown = () => {
    for (const t of tenants.values()) t.loop.stop();
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
