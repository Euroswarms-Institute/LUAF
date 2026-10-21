/** One run: brief, research, design, validate, launch, bundle.
 *
 *  The shape the Python pipeline had, with the publish step pointed at our own
 *  ground and a bundle that charges for itself. Validation failures are fed
 *  back into the designer rather than ending the run, which is where most of
 *  the Python pipeline's retry budget went too. */

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { LauncherDeployment, LlmConfig, RunConfig } from "./config.js";
import { design, generateBrief, DesignError } from "./designer.js";
import { slug, type Product } from "./payload.js";
import { loadProfile, type Profile } from "./profiles.js";
import { research, type Snippet } from "./research.js";
import { validate, type ValidateResult } from "./validate.js";
import { publish, type PublishResult } from "./publish/evm.js";
import { endpointConfigFromEnv, writeBundle, type BundleResult, type EndpointConfig } from "./publish/endpoint.js";
import { specId, type LaunchSpec } from "./spec.js";

export class PipelineError extends Error {}

export interface PipelineRequest {
  spec: LaunchSpec;
  dep: LauncherDeployment;
  llm: LlmConfig;
  run: RunConfig;
  /** ETH/USD with 8 decimals, inside the factory's band. */
  ethUsd8: bigint;
  /** Skip the chain entirely: design and validate only. */
  designOnly?: boolean;
  /** Where the paid endpoint sends its revenue. Defaults to the creator
   *  splitter, so endpoint income splits the same 90/10 as creator fees. */
  endpoint?: EndpointConfig;
  log?: (line: string) => void;
  signal?: AbortSignal;
}

export interface PipelineResult {
  spec: LaunchSpec;
  specId: string;
  brief: string;
  profile: Profile;
  snippets: number;
  product: Product;
  lines: number;
  rejected: string[];
  validation: ValidateResult;
  publish?: PublishResult;
  bundle?: BundleResult;
  dir: string;
}

const noop = () => {};

/** Run the pipeline once for one spec. */
export async function runOnce(req: PipelineRequest): Promise<PipelineResult> {
  const log = req.log ?? noop;
  const { spec, run } = req;
  const profile = loadProfile(spec.profile);
  const id = specId(spec);

  const brief = spec.topic.trim() || (await generateBrief(req.llm, profile, req.signal));
  if (!brief) throw new PipelineError("no brief: the topic was empty and none could be generated");
  log(`brief: ${brief}`);

  const runDir = join(run.outDir, `${Date.now()}-${slug(brief, 32)}`);
  mkdirSync(runDir, { recursive: true });

  let snippets: Snippet[] = [];
  let product: Product | undefined;
  let lines = 0;
  const rejected: string[] = [];
  let validation: ValidateResult | undefined;
  let feedback: string[] = [];

  for (let step = 0; step < run.maxSteps; step++) {
    // Vary the search each attempt, so a second try sees different context
    // rather than re-reading the page that produced the rejected design.
    snippets = await research(brief, { variant: step, hops: step > 0 ? 1 : 0 });
    log(`research: ${snippets.length} snippets (attempt ${step + 1}/${run.maxSteps})`);

    let designed;
    try {
      designed = await design(req.llm, {
        profile,
        brief,
        snippets,
        minLines: run.minLines,
        priorFeedback: feedback,
        signal: req.signal,
      });
    } catch (e) {
      if (e instanceof DesignError) {
        rejected.push(...e.attempts);
        feedback = e.attempts.slice(-1);
        continue;
      }
      throw e;
    }
    rejected.push(...designed.rejected);
    product = designed.product;
    lines = designed.lines;
    log(`designed ${product.name} (${product.ticker}), ${lines} lines`);

    validation = await validate({
      code: product.code,
      language: product.language,
      dir: join(runDir, "validate"),
      timeoutMs: run.validationTimeoutMs,
    });
    if (validation.ok) {
      log(`validated: ran clean${validation.installed.length ? `, installed ${validation.installed.join(", ")}` : ""}`);
      break;
    }
    log(`validation failed: ${validation.complaints[0]?.split("\n")[0] ?? "unknown"}`);
    feedback = validation.complaints;
    product = undefined;
  }

  if (!product || !validation?.ok) {
    writeFileSync(join(runDir, "rejected.json"), JSON.stringify({ brief, rejected, feedback }, null, 2));
    throw new PipelineError(
      `no agent passed design and validation in ${run.maxSteps} attempts; see ${join(runDir, "rejected.json")}`,
    );
  }

  const result: PipelineResult = {
    spec,
    specId: id,
    brief,
    profile,
    snippets: snippets.length,
    product,
    lines,
    rejected,
    validation,
    dir: runDir,
  };

  if (!req.designOnly) {
    result.publish = await publish({ spec, product, dep: req.dep, run, ethUsd8: req.ethUsd8 });
    log(
      result.publish.dryRun
        ? `dry run: would launch ${result.publish.token} (salt ${result.publish.salt.slice(0, 10)}…)`
        : `launched ${result.publish.token} in ${result.publish.txHash}`,
    );
  }

  // The endpoint pays to the creator splitter by default, so service revenue
  // is split on the same terms as the coin's creator fees.
  const endpoint = req.endpoint ?? endpointConfigFromEnv(req.dep.contracts.creatorSplitter);
  result.bundle = writeBundle({
    product,
    dir: join(runDir, "endpoint"),
    endpoint,
    token: result.publish?.token,
    specId: id,
  });
  log(`bundle: ${result.bundle.dir}`);

  writeFileSync(
    join(runDir, "run.json"),
    JSON.stringify(
      {
        specId: id,
        spec: { ...spec, firstBuyWei: spec.firstBuyWei.toString() },
        brief,
        product: { ...product, code: undefined, raw: undefined },
        lines,
        rejected,
        publish: result.publish
          ? {
              ...result.publish,
              firstBuyWei: result.publish.firstBuyWei.toString(),
              tokensOut: result.publish.tokensOut?.toString(),
            }
          : null,
      },
      null,
      2,
    ),
  );
  return result;
}
