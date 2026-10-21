/** The designer: brief plus research in, one agent out.
 *
 *  A port of `get_agent_payload_from_llm` in the Python pipeline, minus the
 *  framework and ReAct wrappers it could optionally route through. It keeps the
 *  part that mattered: a revision loop that feeds the previous attempt's
 *  complaints back in, so a near miss is corrected instead of rerolled. */

import { chat, type Message } from "./llm.js";
import type { LlmConfig } from "./config.js";
import { codeComplaints, parseProduct, substantiveLines, type Product } from "./payload.js";
import { designerSystemPrompt, type Profile } from "./profiles.js";
import { renderSnippets, type Snippet } from "./research.js";

export class DesignError extends Error {
  constructor(message: string, readonly attempts: string[] = []) {
    super(message);
  }
}

export interface DesignRequest {
  profile: Profile;
  brief: string;
  snippets: Snippet[];
  minLines: number;
  /** Design attempts before giving up. Each one after the first carries the
   *  previous attempt's complaints. */
  attempts?: number;
  /** Complaints from a validation run of an earlier design, fed back in. */
  priorFeedback?: string[];
  signal?: AbortSignal;
}

export interface DesignResult {
  product: Product;
  /** What each rejected attempt was rejected for, oldest first. */
  rejected: string[];
  lines: number;
}

function userMessage(req: DesignRequest, feedback: string[]): string {
  const parts = [`## Brief\n${req.brief.trim()}`];
  const context = renderSnippets(req.snippets);
  if (context) {
    parts.push(
      `## Search context\nReal results for this brief. Use the concrete APIs, endpoints and library ` +
        `idioms named here; do not invent endpoints.\n${context}`,
    );
  }
  parts.push(`## Size floor\nAt least ${req.minLines} substantive lines of agent code.`);
  if (feedback.length) {
    parts.push(
      `## Fix these\nYour previous attempt was rejected. Correct every point and return the full JSON ` +
        `object again:\n${feedback.map((f) => `- ${f}`).join("\n")}`,
    );
  }
  return parts.join("\n\n");
}

/** Design one agent, retrying with feedback until it passes the static checks. */
export async function design(cfg: LlmConfig, req: DesignRequest): Promise<DesignResult> {
  const attempts = req.attempts ?? 3;
  const system = designerSystemPrompt(req.profile);
  const rejected: string[] = [];
  let feedback = [...(req.priorFeedback ?? [])];

  for (let i = 0; i < attempts; i++) {
    const messages: Message[] = [
      { role: "system", content: system },
      { role: "user", content: userMessage(req, feedback) },
    ];
    let product: Product;
    try {
      const output = await chat(cfg, messages, { signal: req.signal });
      product = parseProduct(output);
    } catch (e) {
      const why = (e as Error).message;
      rejected.push(why);
      feedback = [`the previous response could not be used: ${why}`];
      continue;
    }
    const complaints = codeComplaints(product, req.minLines);
    if (complaints.length === 0) {
      return { product, rejected, lines: substantiveLines(product.code, product.language) };
    }
    rejected.push(complaints.join("; "));
    feedback = complaints;
  }
  throw new DesignError(`no usable design in ${attempts} attempts`, rejected);
}

/** One brief, when the spec leaves the topic blank. */
export async function generateBrief(cfg: LlmConfig, profile: Profile, signal?: AbortSignal): Promise<string> {
  const text = await chat(
    cfg,
    [
      {
        role: "system",
        content:
          "You generate one concrete, monetizable autonomous business idea. Reply with a single " +
          "sentence and nothing else: no quotes, no preamble, no bullet points.",
      },
      { role: "user", content: profile.topicPrompt },
    ],
    { maxTokens: 200, temperature: 0.8, signal },
  );
  // Models sometimes answer with a quoted sentence or a leading bullet.
  return text.trim().replace(/^["'`\-*\s]+|["'`\s]+$/g, "").split("\n")[0]!.trim();
}
