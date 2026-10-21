/** Designer profiles: the prompt set that decides what kind of unit gets
 *  designed. Each directory under `data/profiles` holds
 *  `system_prompt.txt`, `topic_prompt.txt`, `product_focus.txt` and
 *  `display_name.txt`, carried over unchanged from the Python pipeline so the
 *  tuning in them survives the rewrite. */

import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { raw } from "./config.js";

export class ProfileError extends Error {}

export interface Profile {
  id: string;
  displayName: string;
  systemPrompt: string;
  topicPrompt: string;
  productFocus: string;
}

/** `data/` ships beside the built `dist/`, so resolve it from this module
 *  rather than the working directory. LUAF_PROFILES_DIR overrides it. */
export function profilesDir(): string {
  const override = raw("LUAF_PROFILES_DIR");
  if (override) return isAbsolute(override) ? override : resolve(process.cwd(), override);
  const here = dirname(fileURLToPath(import.meta.url));
  for (const c of [join(here, "../data/profiles"), join(here, "../../data/profiles")]) {
    if (existsSync(c)) return c;
  }
  throw new ProfileError("no profiles directory found; set LUAF_PROFILES_DIR");
}

export function listProfiles(): string[] {
  const dir = profilesDir();
  return readdirSync(dir)
    .filter((n) => !n.startsWith(".") && statSync(join(dir, n)).isDirectory())
    .filter((n) => existsSync(join(dir, n, "system_prompt.txt")))
    .sort();
}

function readIfPresent(path: string): string {
  return existsSync(path) ? readFileSync(path, "utf8").trim() : "";
}

export function loadProfile(id: string): Profile {
  const dir = join(profilesDir(), id);
  const systemPrompt = readIfPresent(join(dir, "system_prompt.txt"));
  if (!systemPrompt) {
    throw new ProfileError(`profile ${JSON.stringify(id)} has no system_prompt.txt (have: ${listProfiles().join(", ")})`);
  }
  return {
    id,
    displayName: readIfPresent(join(dir, "display_name.txt")) || id,
    systemPrompt,
    topicPrompt: readIfPresent(join(dir, "topic_prompt.txt")) || DEFAULT_TOPIC_PROMPT,
    productFocus: readIfPresent(join(dir, "product_focus.txt")),
  };
}

export const DEFAULT_TOPIC_PROMPT =
  "Generate exactly one concrete, autonomous business idea that is monetizable and tokenizable. " +
  "It must make money without a frontend: API usage, token fees, data or arbitrage output, automated " +
  "backends. Reply with only that one sentence, no quotes, no explanation, no bullet points.";

/** The output contract. Ported from the Python pipeline's publication
 *  fragment, with two changes: the Swarms SDK is no longer a mandatory
 *  dependency (that target is gone), and `is_free` is dropped because it only
 *  ever meant something to the marketplace listing. */
export const OUTPUT_FORMAT_FRAGMENT = `
## JSON output format (mandatory)
Your entire response must be one JSON object: no characters before the opening { or after the closing }.

Forbidden: any text, reasoning or explanation before the opening {. Any text or summary after the closing }.
Markdown code fences. Trailing commas. Placeholder or empty values for required keys. Any key holding a
private key, mnemonic or wallet secret.

Suggested key order: name, ticker, description, agent, useCases, tags, requirements, language.

Required top-level keys (exactly these):
- name (string)
- ticker (string, 2-12 characters, uppercase A-Z and digits only)
- description (string)
- agent (string: the full source of the agent; literal newlines in the string are fine)
- useCases (array of {"title": string, "description": string}); at least 3 items
- tags (string, comma-separated; no comma inside a tag)
- requirements (array of {"package": string, "installation": string}); list every third-party package the
  code imports, and nothing it does not
- language (string)

Do not mandate any particular agent framework. Import only what the code actually uses.
`.trim();

/** The runtime contract. The agent is wrapped as a paid HTTP endpoint that
 *  runs the script as a subprocess, so it has to do one unit of work and exit.
 *  Ported from the Python pipeline's RapidAPI suffix, which is the only
 *  packaging that survived. */
export const RUNTIME_FRAGMENT = `
## Runtime contract (mandatory)
The agent is wrapped as an HTTP endpoint that runs your script as a subprocess and charges per call, so:
- It must run successfully with zero arguments, performing one default unit of work.
- It must accept a task through an optional argument, e.g. \`--task "..."\`, so the endpoint can forward
  \`["--task", "<caller input>"]\`.
- It must not block forever by default: no infinite loop unless an explicit flag asks for one.
- Keep \`main()\` thin: parse arguments, delegate to named functions, exit with a non-zero status on failure.
- Read every credential from the environment (never a literal), and comment where to obtain each one.
- Prefer APIs that need no key at all. Where a key is unavoidable, the code must degrade to a clear error
  rather than crash.
`.trim();

/** Assemble the designer's system prompt for a profile. */
export function designerSystemPrompt(profile: Profile): string {
  return [profile.systemPrompt, profile.productFocus, RUNTIME_FRAGMENT, OUTPUT_FORMAT_FRAGMENT]
    .filter((s) => s.trim())
    .join("\n\n");
}
