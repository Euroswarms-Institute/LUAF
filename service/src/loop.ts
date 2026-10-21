/** The persistent loop, as a worker the service starts and stops.
 *
 *  As a CLI command this was a process you watched. As infrastructure it is a
 *  single worker per tenant: at most one runs at a time, because two loops
 *  breeding against the same population file would each overwrite the other's
 *  generation and selection would stop meaning anything.
 *
 *  A running loop holds its tenant's budget reservation — the worst case for
 *  every launch it is allowed — and gives back the difference when it stops.
 *  That is why the reservation travels with the loop rather than being settled
 *  by the route that started it: the route returns in milliseconds and the loop
 *  runs for hours. */

import { runPersistent, type LauncherDeployment, type LaunchSpec, type LlmConfig, type RunConfig } from "@lucreocracy/core";

import { spentByLoop } from "./spend.js";
import type { Budget, Reservation } from "./tenants.js";

export interface LoopState {
  running: boolean;
  startedAt?: number;
  finishedAt?: number;
  launches: number;
  /** Why it ended: the loop's own reason, or an error. */
  stopped?: string;
  note?: string;
  incomeEthWei?: string;
  spentEthWei?: string;
}

export interface LoopDeps {
  dep: LauncherDeployment;
  llm: LlmConfig;
  run: RunConfig;
  ethUsd8: bigint;
  targetEthWei: bigint;
  maxLaunches: number;
  budget: Budget;
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

  start(args: { spec: LaunchSpec; reservation?: Reservation; reservedLaunches: number }): {
    ok: boolean;
    message: string;
  } {
    if (this.state.running) return { ok: false, message: "the loop is already running" };

    const controller = new AbortController();
    this.controller = controller;
    this.state = { running: true, startedAt: Date.now(), launches: 0 };
    const log = (line: string) => {
      this.deps.log?.(`[loop] ${line}`);
    };

    void runPersistent({
      seed: args.spec,
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
        const spent = spentByLoop(result);
        if (args.reservation) {
          this.deps.budget.settle(args.reservation, spent.wei, spent.launches, args.reservedLaunches);
        }
        this.state = {
          running: false,
          startedAt: this.state.startedAt,
          finishedAt: Date.now(),
          launches: result.launches,
          stopped: result.stopped,
          incomeEthWei: result.incomeEthWei.toString(),
          spentEthWei: spent.wei.toString(),
          note:
            result.stopped === "glutted"
              ? "stopped because the market stopped absorbing, not because it ran out of work"
              : undefined,
        };
      })
      .catch((e: unknown) => {
        // The loop threw, so how much it signed for is not known from its
        // result. Charge the launches it reported and keep the rest reserved
        // until the ledger is reconciled against the chain; refunding a run
        // that may have launched is the one direction that costs us ETH.
        if (args.reservation) {
          const launches = this.state.launches;
          this.deps.budget.settle(
            args.reservation,
            args.reservation.wei,
            Math.max(launches, 1),
            args.reservedLaunches,
          );
        }
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
