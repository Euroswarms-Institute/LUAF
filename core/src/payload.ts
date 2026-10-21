/** Parsing and checking the designer's JSON.
 *
 *  A port of the Python pipeline's designer payload parser. The designer is told to answer with one
 *  JSON object and nothing else, and mostly does; this recovers the object when
 *  it wraps it in a code fence, prefixes it with prose, or leaves a trailing
 *  comma. Anything looser is a failure the designer loop retries, not something
 *  to repair further: a half-parsed agent is worse than another attempt. */

export const REQUIRED_KEYS = [
  "name",
  "agent",
  "description",
  "language",
  "requirements",
  "useCases",
  "tags",
  "ticker",
] as const;

export class PayloadError extends Error {}

export interface UseCase {
  title: string;
  description: string;
}

export interface Requirement {
  package: string;
  installation: string;
}

/** The designer's output, normalised. Mirrors the legacy canonical model so
 *  the shipped profile prompts keep working unchanged. */
export interface Product {
  name: string;
  ticker: string;
  description: string;
  /** The agent's source. */
  code: string;
  language: string;
  useCases: UseCase[];
  requirements: Requirement[];
  tags: string;
  raw: Record<string, unknown>;
}

function stripCodeFence(text: string): string {
  let t = text.trim();
  if (!t.startsWith("```")) return t;
  const nl = t.indexOf("\n");
  if (nl !== -1) t = t.slice(nl + 1);
  const close = t.lastIndexOf("```");
  if (close !== -1) t = t.slice(0, close);
  return t.trim();
}

/** The first balanced {...} run, ignoring braces inside strings. */
function firstJsonObject(text: string): string {
  let depth = 0;
  let start = -1;
  let inStr = false;
  let quote = "";
  let esc = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i]!;
    if (esc) {
      esc = false;
      continue;
    }
    if (inStr && c === "\\") {
      esc = true;
      continue;
    }
    if (c === '"' || c === "'") {
      if (!inStr) {
        inStr = true;
        quote = c;
      } else if (c === quote) {
        inStr = false;
        quote = "";
      }
      continue;
    }
    if (inStr) continue;
    if (c === "{") {
      if (depth === 0) start = i;
      depth++;
    } else if (c === "}" && depth > 0) {
      depth--;
      if (depth === 0 && start !== -1) return text.slice(start, i + 1);
    }
  }
  return "";
}

function dropTrailingCommas(s: string): string {
  return s.replace(/,(\s*[}\]])/g, "$1");
}

function asString(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/** Parse, check the required keys are present, and normalise. */
export function parseProduct(output: string): Product {
  if (!output?.trim()) throw new PayloadError("empty designer output");
  const js = firstJsonObject(stripCodeFence(output));
  if (!js) throw new PayloadError("no JSON object in designer output");
  let parsed: unknown;
  try {
    parsed = JSON.parse(dropTrailingCommas(js));
  } catch (e) {
    throw new PayloadError(`invalid JSON: ${(e as Error).message}`);
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new PayloadError("designer output is not a JSON object");
  }
  const raw = parsed as Record<string, unknown>;
  const missing = REQUIRED_KEYS.filter((k) => !(k in raw));
  if (missing.length) throw new PayloadError(`missing keys: ${missing.join(", ")}`);

  const useCases: UseCase[] = Array.isArray(raw.useCases)
    ? raw.useCases
        .filter((u): u is Record<string, unknown> => typeof u === "object" && u !== null)
        .map((u) => ({ title: asString(u.title), description: asString(u.description) }))
    : [];
  const requirements: Requirement[] = Array.isArray(raw.requirements)
    ? raw.requirements
        .filter((r): r is Record<string, unknown> => typeof r === "object" && r !== null)
        .map((r) => ({ package: asString(r.package), installation: asString(r.installation) }))
    : [];

  const product: Product = {
    name: asString(raw.name).trim(),
    ticker: asString(raw.ticker).trim().toUpperCase(),
    description: asString(raw.description).trim(),
    code: asString(raw.agent),
    language: (asString(raw.language).trim() || "python").toLowerCase(),
    useCases,
    requirements,
    tags: asString(raw.tags).trim(),
    raw,
  };
  if (!product.name) throw new PayloadError("name is empty");
  if (!/^[A-Z0-9]{2,12}$/.test(product.ticker)) {
    throw new PayloadError(`ticker ${JSON.stringify(product.ticker)} must be 2-12 chars, A-Z and 0-9`);
  }
  if (!product.code.trim()) throw new PayloadError("agent code is empty");
  return product;
}

/** Lines that are neither blank nor a whole-line comment. The legacy floor was
 *  measured this way, and the profile prompts quote it to the designer. */
export function substantiveLines(code: string, language = "python"): number {
  const comment = language === "python" ? /^\s*#/ : /^\s*(\/\/|\/\*|\*)/;
  return code.split("\n").filter((l) => l.trim() !== "" && !comment.test(l)).length;
}

const STUB_MARKERS = [
  "NotImplementedError",
  "TODO:",
  "FIXME",
  "pass  # implement",
  "your_api_key_here",
  "<YOUR_",
  "example.com/api",
  "lorem ipsum",
];

/** Reasons to reject generated code before it is ever run. Returns the
 *  complaints, so the designer loop can feed them back as a revision note. */
export function codeComplaints(product: Product, minLines: number): string[] {
  const out: string[] = [];
  const lines = substantiveLines(product.code, product.language);
  if (lines < minLines) out.push(`only ${lines} substantive lines, needs at least ${minLines}`);
  for (const m of STUB_MARKERS) {
    if (product.code.includes(m)) out.push(`contains the placeholder ${JSON.stringify(m)}`);
  }
  if (!/def |class |function |const |async /.test(product.code)) {
    out.push("no function or class definitions found");
  }
  return out;
}

/** A filesystem-safe slug for run directories and bundle names. */
export function slug(s: string, maxLen = 48): string {
  const t = (s || "agent")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return (t || "agent").slice(0, maxLen);
}
