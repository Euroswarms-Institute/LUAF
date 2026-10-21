/** Minimal OpenAI-compatible chat client.
 *
 *  The legacy pipeline reached the same endpoint three ways (a Swarms Agent, a
 *  ReAct loop, and a direct POST) and defaulted to the direct one. Only the
 *  direct path survives: the designer is a single long completion, so an agent
 *  framework bought nothing and pinned an extra dependency. */

import type { LlmConfig } from "./config.js";

export interface Message {
  role: "system" | "user" | "assistant";
  content: string;
}

export class LlmError extends Error {
  constructor(message: string, readonly status?: number, readonly retryable = false) {
    super(message);
  }
}

export interface ChatOptions {
  maxTokens?: number;
  temperature?: number;
  /** Attempts in total, including the first. */
  attempts?: number;
  signal?: AbortSignal;
}

const RETRYABLE_STATUS = new Set([408, 409, 425, 429, 500, 502, 503, 504, 529]);

/** One completion, with backoff on the failures that are worth retrying.
 *  A long designer call is expensive, so a 400 (a bad request, a prompt over
 *  the context window) fails immediately rather than three times. */
export async function chat(cfg: LlmConfig, messages: Message[], opts: ChatOptions = {}): Promise<string> {
  const attempts = opts.attempts ?? 3;
  let lastError: LlmError | undefined;
  for (let i = 0; i < attempts; i++) {
    if (i > 0) await sleep(Math.min(30_000, 1_000 * 2 ** (i - 1)));
    try {
      return await once(cfg, messages, opts);
    } catch (e) {
      const err = e instanceof LlmError ? e : new LlmError(String((e as Error).message ?? e), undefined, true);
      if (!err.retryable) throw err;
      lastError = err;
    }
  }
  throw lastError ?? new LlmError("no attempts made");
}

async function once(cfg: LlmConfig, messages: Message[], opts: ChatOptions): Promise<string> {
  const timeout = AbortSignal.timeout(cfg.timeoutMs);
  const signal = opts.signal ? AbortSignal.any([timeout, opts.signal]) : timeout;
  let res: Response;
  try {
    res = await fetch(`${cfg.baseUrl}/chat/completions`, {
      method: "POST",
      headers: { authorization: `Bearer ${cfg.apiKey}`, "content-type": "application/json" },
      body: JSON.stringify({
        model: cfg.model,
        messages,
        temperature: opts.temperature ?? cfg.temperature,
        max_tokens: opts.maxTokens ?? cfg.maxTokens,
      }),
      signal,
    });
  } catch (e) {
    // A network failure or a timeout: both worth another try.
    throw new LlmError(`request failed: ${(e as Error).message}`, undefined, true);
  }
  if (!res.ok) {
    const body = (await res.text().catch(() => "")).slice(0, 2_000);
    throw new LlmError(`HTTP ${res.status}: ${body}`, res.status, RETRYABLE_STATUS.has(res.status));
  }
  const json = (await res.json()) as { choices?: { message?: { content?: string } }[] };
  const text = json.choices?.[0]?.message?.content;
  // An empty completion is usually a transient refusal to produce output; the
  // next attempt at a high temperature often succeeds.
  if (!text?.trim()) throw new LlmError("empty completion", undefined, true);
  return text;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
