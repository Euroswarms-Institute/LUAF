/** Research: real-world context for the designer.
 *
 *  The Python pipeline used the `ddgs` package. This talks to the same HTML
 *  endpoint directly, which removes a dependency and makes failure explicit:
 *  research is best-effort, and an empty result is a thinner prompt rather than
 *  a failed run. Searches are biased toward keyless APIs, as before, because an
 *  agent that needs no credential can actually run once it is launched. */

const ENDPOINT = "https://html.duckduckgo.com/html/";
const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36";

export interface Snippet {
  title: string;
  body: string;
  url: string;
}

function decodeEntities(s: string): string {
  return s
    .replace(/<[^>]*>/g, "")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;|&#39;|&apos;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&nbsp;/g, " ")
    .replace(/&#(\d+);/g, (_, d: string) => String.fromCharCode(Number(d)))
    .replace(/\s+/g, " ")
    .trim();
}

/** DuckDuckGo's HTML result list. The markup is not a contract, so a layout
 *  change degrades to zero results instead of throwing. */
function parseResults(html: string, max: number): Snippet[] {
  const out: Snippet[] = [];
  const re = /<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>([\s\S]*?)(?=<a[^>]+class="result__a"|$)/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(html)) && out.length < max) {
    const url = decodeEntities(m[1] ?? "");
    const title = decodeEntities(m[2] ?? "");
    const tail = m[3] ?? "";
    const snippet = /class="result__snippet"[^>]*>([\s\S]*?)<\/a>/.exec(tail);
    const body = decodeEntities(snippet?.[1] ?? "");
    if (title) out.push({ title, body, url });
  }
  return out;
}

export interface SearchOptions {
  maxResults?: number;
  timeoutMs?: number;
}

const cache = new Map<string, Snippet[]>();

export async function search(query: string, opts: SearchOptions = {}): Promise<Snippet[]> {
  const q = query.trim();
  if (!q) return [];
  const max = opts.maxResults ?? 10;
  const key = `${max}:${q}`;
  const hit = cache.get(key);
  if (hit) return hit;
  try {
    const res = await fetch(ENDPOINT, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", "user-agent": UA },
      body: new URLSearchParams({ q, kl: "wt-wt" }).toString(),
      signal: AbortSignal.timeout(opts.timeoutMs ?? 20_000),
    });
    if (!res.ok) return [];
    const results = parseResults(await res.text(), max);
    cache.set(key, results);
    return results;
  } catch {
    // Research is optional context. A blocked or slow endpoint must not take
    // down a run that can still design from the brief alone.
    return [];
  }
}

export function renderSnippets(snippets: Snippet[]): string {
  return snippets.map((s) => `${s.title}: ${s.body}`.trim()).join("\n");
}

/** Rotated suffixes, so a repeated brief does not return the identical page.
 *  The Python pipeline did the same with SEARCH_VARIANT_SUFFIX. */
const VARIANTS = [
  "",
  "how it works",
  "API documentation",
  "pricing and limits",
  "open source implementation",
  "market size 2026",
];

export interface ResearchOptions extends SearchOptions {
  /** Also search for keyless/public APIs and append the results. */
  keylessApis?: boolean;
  /** Follow up with queries built from the first page's titles. */
  hops?: number;
  /** Index into the suffix rotation; the caller varies it per attempt. */
  variant?: number;
}

/** Gather context for a brief: one base search, an optional keyless-API
 *  search, and optional extra hops seeded from what came back. */
export async function research(brief: string, opts: ResearchOptions = {}): Promise<Snippet[]> {
  const b = brief.trim();
  if (!b) return [];
  const suffix = VARIANTS[(opts.variant ?? 0) % VARIANTS.length] ?? "";
  const seen = new Set<string>();
  const all: Snippet[] = [];
  const add = (snippets: Snippet[]) => {
    for (const s of snippets) {
      const k = s.url || s.title;
      if (seen.has(k)) continue;
      seen.add(k);
      all.push(s);
    }
  };

  add(await search(`${b} ${suffix}`.trim(), opts));
  if (opts.keylessApis ?? true) {
    add(await search(`${b} free public API no API key`, opts));
  }
  // Each hop asks about one thing the first page surfaced, which is cheaper
  // and steadier than embedding-guided hops for the same purpose: more
  // concrete nouns in the designer's context.
  const hops = Math.max(0, Math.min(opts.hops ?? 0, 3));
  for (let i = 0; i < hops; i++) {
    const seed = all[i]?.title;
    if (!seed) break;
    add(await search(`${seed} ${b}`.slice(0, 180), { ...opts, maxResults: 5 }));
  }
  return all;
}

/** One brief from the profile's topic prompt, for when none was given. */
export function topicMessages(topicPrompt: string): { system: string; user: string } {
  return {
    system:
      "You generate one concrete, monetizable autonomous business idea. Reply with a single sentence " +
      "and nothing else: no quotes, no preamble, no bullet points.",
    user: topicPrompt,
  };
}
