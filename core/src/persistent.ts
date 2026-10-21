/** The loop: launch, measure, breed, launch again.
 *
 *  The Python pipeline looped until a wallet balance hit a target and chose
 *  each topic from a list. This loop is closed instead: after every launch it
 *  reads what the previous ones actually earned, folds that into the
 *  population, and takes the next spec from selection. The stop condition is
 *  ETH income, which is the thing the business is for.
 *
 *  It also stops when there is nothing worth launching. If selection has
 *  reaped every spec — every format saturated, none earning more than a launch
 *  costs — then continuing to launch is paying to add copies to a market that
 *  pays nothing for them. */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import type { LauncherDeployment, LlmConfig, RunConfig } from "./config.js";
import { collectIncome, bySpec } from "./income.js";
import { runOnce, type PipelineResult } from "./pipeline.js";
import {
  applyIncome,
  breed,
  defaultSelectionConfig,
  fitness,
  nextSpec,
  parsePopulation,
  reap,
  seedPopulation,
  serializePopulation,
  type Population,
  type SelectionConfig,
} from "./selection.js";
import type { LaunchSpec } from "./spec.js";

export interface PersistentRequest {
  seed: LaunchSpec;
  dep: LauncherDeployment;
  llm: LlmConfig;
  run: RunConfig;
  ethUsd8: bigint;
  /** Stop once measured income reaches this, in wei. */
  targetEthWei: bigint;
  /** Hard ceiling on launches, so a loop cannot run away. */
  maxLaunches: number;
  /** Launches between breeding steps. */
  epochEvery?: number;
  /** Where the population is kept between runs. */
  populationPath?: string;
  selection?: SelectionConfig;
  log?: (line: string) => void;
  signal?: AbortSignal;
}

export interface PersistentResult {
  launches: number;
  failures: number;
  epochs: number;
  incomeEthWei: bigint;
  population: Population;
  stopped: "target" | "max-launches" | "exhausted" | "aborted";
  results: PipelineResult[];
}

const noop = () => {};

function loadPopulation(path: string | undefined, seed: LaunchSpec): Population {
  if (path && existsSync(path)) {
    try {
      const pop = parsePopulation(readFileSync(path, "utf8"));
      if (pop.members.length) return pop;
    } catch {
      // A corrupt file must not strand the loop; start over from the seed.
    }
  }
  return seedPopulation(seed);
}

function savePopulation(path: string | undefined, pop: Population): void {
  if (!path) return;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, serializePopulation(pop), "utf8");
}

export async function runPersistent(req: PersistentRequest): Promise<PersistentResult> {
  const log = req.log ?? noop;
  const cfg = req.selection ?? defaultSelectionConfig;
  const epochEvery = Math.max(1, req.epochEvery ?? 4);
  let pop = loadPopulation(req.populationPath, req.seed);
  const results: PipelineResult[] = [];
  let launches = 0;
  let failures = 0;
  let epochs = 0;
  let incomeEthWei = 0n;
  let stopped: PersistentResult["stopped"] = "max-launches";

  /** Read the chain and fold what it says into the population. */
  const measure = async (): Promise<bigint> => {
    if (req.run.dryRun) return 0n; // nothing was launched, so nothing earned
    const rows = await collectIncome(req.dep, req.run.rpcUrl);
    const agg = bySpec(rows);
    pop = applyIncome(pop, agg);
    let total = 0n;
    for (const row of agg.values()) total += row.totalEthWei;
    return total;
  };

  while (launches < req.maxLaunches) {
    if (req.signal?.aborted) {
      stopped = "aborted";
      break;
    }
    const member = nextSpec(pop, cfg);
    if (!member) {
      stopped = "exhausted";
      break;
    }

    const spec: LaunchSpec = { ...member.spec, tenant: req.dep.tenant, chainId: req.dep.chainId };
    log(`--- launch ${launches + 1}/${req.maxLaunches}  spec ${member.id.slice(0, 10)}…  profile ${spec.profile}`);
    try {
      const result = await runOnce({
        spec,
        dep: req.dep,
        llm: req.llm,
        run: req.run,
        ethUsd8: req.ethUsd8,
        log,
        signal: req.signal,
      });
      results.push(result);
      member.copies++;
      member.lastLaunch = pop.epoch;
      launches++;
    } catch (e) {
      failures++;
      log(`launch failed: ${(e as Error).message}`);
      // A spec that cannot produce a working agent is charged for the attempt:
      // its copy count rises with no income, so selection reaps it in time.
      member.copies++;
      if (failures >= req.maxLaunches) break;
    }
    savePopulation(req.populationPath, pop);

    incomeEthWei = await measure();
    log(`income so far: ${incomeEthWei} wei`);
    if (req.targetEthWei > 0n && incomeEthWei >= req.targetEthWei) {
      stopped = "target";
      break;
    }

    if (launches % epochEvery === 0) {
      const before = pop.members.length;
      pop = breed(pop, { cfg, tenant: req.dep.tenant, chainId: req.dep.chainId });
      epochs++;
      const best = pop.members.length ? fitness(pop, pop.members[0]!) : 0n;
      log(`epoch ${pop.epoch}: ${before} -> ${pop.members.length} specs, best fitness ${best} wei/copy`);
      savePopulation(req.populationPath, pop);
      if (pop.members.length === 0) {
        stopped = "exhausted";
        break;
      }
    }
  }

  // One last reap, so the saved population reflects what is still worth
  // launching next time rather than what was tried this time.
  pop = { ...pop, members: reap(pop, cfg) };
  savePopulation(req.populationPath, pop);
  return { launches, failures, epochs, incomeEthWei, population: pop, stopped, results };
}
