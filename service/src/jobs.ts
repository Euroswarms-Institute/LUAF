/** Pipeline runs as jobs, because a run is minutes long.
 *
 *  The CLI could block a terminal for as long as it liked. An HTTP request
 *  cannot: a design pass makes several long LLM calls and then runs generated
 *  code in a subprocess. So `POST /runs` starts a job and returns its id, and
 *  the caller polls.
 *
 *  Jobs live in memory and the pipeline writes its own directory per run, so
 *  history survives a restart on disk even though in-flight work does not.
 *  That is the honest division: a restart loses what was running, and says so,
 *  rather than pretending a queue it does not have. */

import { randomUUID } from "node:crypto";

import type { PipelineResult } from "@lucreocracy/core";

export type JobState = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: string;
  state: JobState;
  /** True when this job was allowed to sign. */
  live: boolean;
  startedAt: number;
  finishedAt?: number;
  /** Progress lines the pipeline emitted, newest last. */
  log: string[];
  result?: {
    specId: string;
    brief: string;
    name: string;
    ticker: string;
    lines: number;
    token?: string;
    txHash?: string;
    dryRun: boolean;
    dir: string;
    bundleDir?: string;
  };
  error?: string;
}

/** What a job looks like over the wire: the log is capped so a poll cannot
 *  return megabytes, and nothing secret is ever in a job. */
export interface JobView extends Omit<Job, "log"> {
  log: string[];
  logTruncated: boolean;
}

const MAX_LOG_LINES = 500;

export class Jobs {
  private readonly jobs = new Map<string, Job>();
  private readonly aborts = new Map<string, AbortController>();
  /** Most recent first. */
  private readonly order: string[] = [];

  constructor(private readonly keep = 100) {}

  /** Start a run. The runner is injected so the routes stay testable without
   *  an LLM, a chain or a key. */
  start(opts: { live: boolean; run: (ctx: { log: (line: string) => void; signal: AbortSignal }) => Promise<PipelineResult> }): Job {
    const id = randomUUID();
    const job: Job = { id, state: "queued", live: opts.live, startedAt: Date.now(), log: [] };
    this.jobs.set(id, job);
    this.order.unshift(id);
    this.prune();

    const controller = new AbortController();
    this.aborts.set(id, controller);
    const log = (line: string) => {
      if (job.log.length < MAX_LOG_LINES) job.log.push(line);
    };

    job.state = "running";
    // Deliberately not awaited: the caller gets the id immediately.
    void opts
      .run({ log, signal: controller.signal })
      .then((result) => {
        job.state = "done";
        job.result = {
          specId: result.specId,
          brief: result.brief,
          name: result.product.name,
          ticker: result.product.ticker,
          lines: result.lines,
          token: result.publish?.token,
          txHash: result.publish?.txHash,
          dryRun: result.publish?.dryRun ?? true,
          dir: result.dir,
          bundleDir: result.bundle?.dir,
        };
      })
      .catch((e: unknown) => {
        job.state = controller.signal.aborted ? "cancelled" : "failed";
        job.error = e instanceof Error ? e.message : String(e);
      })
      .finally(() => {
        job.finishedAt = Date.now();
        this.aborts.delete(id);
      });

    return job;
  }

  get(id: string): Job | undefined {
    return this.jobs.get(id);
  }

  view(job: Job): JobView {
    return { ...job, log: job.log, logTruncated: job.log.length >= MAX_LOG_LINES };
  }

  list(limit = 20): JobView[] {
    return this.order.slice(0, limit).flatMap((id) => {
      const j = this.jobs.get(id);
      return j ? [this.view(j)] : [];
    });
  }

  /** Ask a running job to stop. The pipeline checks the signal between steps,
   *  so this is cooperative rather than immediate. */
  cancel(id: string): boolean {
    const controller = this.aborts.get(id);
    if (!controller) return false;
    controller.abort();
    return true;
  }

  private prune(): void {
    while (this.order.length > this.keep) {
      const id = this.order.pop();
      if (id) this.jobs.delete(id);
    }
  }
}
