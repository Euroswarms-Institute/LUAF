/** The persistent loop, as a worker the service starts and stops.
 *
 *  As a CLI command this was a process you watched. As infrastructure it is a
 *  single worker: at most one runs at a time, because two loops breeding
 *  against the same population file would each overwrite the other's
 *  generation and selection would stop meaning anything. */

import { runPersistent, type LauncherDeployment, type LlmConfig, type RunConfig } from "@lucreocracy/core";

import { specFromRequest, type RunRequest } from "./request.js";

export interface LoopState {
  running: boolean;
  startedAt?: number;
  finishedAt?: number;
  launches: number;
  /** Why it ended: the loop's own reason, or an error. */
  stopped?: string;
  note?: string;
  incomeEthWei?: string;
}

export interface LoopDeps {
  dep: LauncherDeployment;
  llm: LlmConfig;
  run: RunConfig;
  ethUsd8: bigint;
  defaultProfile: string;
  targetEthWei: bigint;
  maxLaunches: number;
  populationPath?: string;
  aggregatorUrl?: string;
  log?: (line: string) => void;
}

export class Loop {
  private controller?: AbortController;
  private state: LoopState = { running: false, launches: 0 };

  constructor(private readonly deps: LoopDeps) {}

  current(): LoopState {
    return { ...this.state };
  }

  start(request: RunRequest): { ok: boolean; message: string } {
    if (this.state.running) return { ok: false, message: "the loop is already running" };
    const spec = specFromRequest(request, {
      tenant: this.deps.dep.tenant,
      chainId: this.deps.dep.chainId,
      defaultProfile: this.deps.defaultProfile,
    });
    if (!spec.ok) return { ok: false, message: spec.error };

    const controller = new AbortController();
    this.controller = controller;
    this.state = { running: true, startedAt: Date.now(), launches: 0 };
    const log = (line: string) => {
      this.deps.log?.(`[loop] ${line}`);
    };

    void runPersistent({
      seed: spec.value,
      dep: this.deps.dep,
      llm: this.deps.llm,
      run: this.deps.run,
      ethUsd8: this.deps.ethUsd8,
      targetEthWei: this.deps.targetEthWei,
      maxLaunches: this.deps.maxLaunches,
      populationPath: this.deps.populationPath,
      aggregatorUrl: this.deps.aggregatorUrl,
      log,
      signal: controller.signal,
    })
      .then((result) => {
        this.state = {
          running: false,
          startedAt: this.state.startedAt,
          finishedAt: Date.now(),
          launches: result.launches,
          stopped: result.stopped,
          incomeEthWei: result.incomeEthWei.toString(),
          note:
            result.stopped === "glutted"
              ? "stopped because the market stopped absorbing, not because it ran out of work"
              : undefined,
        };
      })
      .catch((e: unknown) => {
        this.state = {
          running: false,
          startedAt: this.state.startedAt,
          finishedAt: Date.now(),
          launches: this.state.launches,
          stopped: "error",
          note: e instanceof Error ? e.message : String(e),
        };
      })
      .finally(() => {
        this.controller = undefined;
      });

    return { ok: true, message: "started" };
  }

  stop(): { ok: boolean; message: string } {
    if (!this.controller) return { ok: false, message: "the loop is not running" };
    this.controller.abort();
    // Cooperative: the loop checks the signal between launches, so a design
    // pass already in flight finishes rather than being killed mid-write.
    return { ok: true, message: "stopping after the current launch" };
  }
}
