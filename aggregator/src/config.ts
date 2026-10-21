/** What the aggregator indexes.
 *
 *  `backend/` indexes exactly one launchpad on one chain, because it was built
 *  to serve one. The terminal is the opposite case: it has to see every
 *  launchpad on every chain we care about, including the ones we did not build
 *  and the ones that do not exist yet. So sources are configuration, not code.
 *
 *  Two adapter kinds:
 *
 *  - `venture` — our own venture factories. The ABI is known, so launches,
 *    graduations and fees are all read exactly.
 *  - `generic` — anyone else's launchpad. The source names the event and which
 *    of its arguments carry the coin, the creator and the name, so a new
 *    launchpad is added by describing its event rather than by shipping an
 *    adapter for it. Nothing here guesses at a third-party ABI: an event
 *    signature that does not match simply yields no logs. */

import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";

import * as dotenv from "dotenv";
import { isAddress, type Address } from "viem";

dotenv.config();

export class ConfigError extends Error {}

export type SourceKind = "venture" | "generic";

export interface SourceConfig {
  /** Stable id, used in the database and the API. */
  id: string;
  /** Display name of the launchpad. */
  label: string;
  kind: SourceKind;
  chainId: number;
  /** The factory or launchpad contract to watch. */
  address: Address;
  /** First block worth scanning. */
  startBlock: number;
  /** `generic` only: the launch event, in human-readable ABI form, e.g.
   *  "event TokenCreated(address indexed token, address indexed creator, string name, string symbol)". */
  launchEvent?: string;
  /** `generic` only: which event arguments carry what. Names must match the
   *  signature above. */
  map?: {
    token: string;
    creator?: string;
    name?: string;
    symbol?: string;
  };
  /** `generic` only: an event that means the coin reached an open market. */
  graduateEvent?: string;
  graduateMap?: { token: string };
}

export interface ChainConfig {
  chainId: number;
  label: string;
  rpcUrl: string;
  /** Blocks per getLogs page. Public endpoints cap the span. */
  logChunk?: number;
}

export interface AggregatorConfig {
  chains: ChainConfig[];
  sources: SourceConfig[];
  port: number;
  dbPath: string;
  pollIntervalMs: number;
  /** Window the saturation counts are measured over. */
  windowDays: number;
}

function envInt(name: string, fallback: number): number {
  const v = process.env[name]?.trim();
  if (!v) return fallback;
  if (!/^\d+$/.test(v)) throw new ConfigError(`${name}=${v}: expected an integer`);
  return Number(v);
}

/** Expand `${VAR}` in an RPC URL, so a keyed endpoint stays out of the file. */
function expand(value: string): string {
  return value.replace(/\$\{([A-Z0-9_]+)\}/g, (_, name: string) => {
    const v = process.env[name];
    if (!v) throw new ConfigError(`sources config references ${name}, which is not set`);
    return v;
  });
}

function checkSource(s: SourceConfig, chains: Set<number>): void {
  if (!/^[a-z0-9-]+$/.test(s.id)) throw new ConfigError(`source id ${JSON.stringify(s.id)} must match [a-z0-9-]+`);
  if (!isAddress(s.address)) throw new ConfigError(`source ${s.id}: address is not an address`);
  if (!chains.has(s.chainId)) throw new ConfigError(`source ${s.id}: no chain configured for ${s.chainId}`);
  if (s.kind === "generic") {
    if (!s.launchEvent) throw new ConfigError(`source ${s.id}: a generic source needs launchEvent`);
    if (!s.map?.token) throw new ConfigError(`source ${s.id}: a generic source needs map.token`);
  } else if (s.kind !== "venture") {
    throw new ConfigError(`source ${s.id}: unknown kind ${JSON.stringify(s.kind)}`);
  }
}

export function loadConfig(path?: string): AggregatorConfig {
  const file = path ?? process.env.SOURCES_FILE ?? "sources.json";
  const full = isAbsolute(file) ? file : resolve(process.cwd(), file);
  if (!existsSync(full)) {
    throw new ConfigError(`no sources config at ${full}; copy sources.example.json and edit it`);
  }
  let raw: { chains?: ChainConfig[]; sources?: SourceConfig[] };
  try {
    raw = JSON.parse(readFileSync(full, "utf8")) as typeof raw;
  } catch (e) {
    throw new ConfigError(`${full} is not valid JSON: ${(e as Error).message}`);
  }
  const chains = (raw.chains ?? []).map((c) => ({ ...c, rpcUrl: expand(c.rpcUrl) }));
  if (chains.length === 0) throw new ConfigError(`${full} configures no chains`);
  const ids = new Set(chains.map((c) => c.chainId));
  const sources = raw.sources ?? [];
  if (sources.length === 0) throw new ConfigError(`${full} configures no sources`);
  const seen = new Set<string>();
  for (const s of sources) {
    if (seen.has(s.id)) throw new ConfigError(`duplicate source id ${s.id}`);
    seen.add(s.id);
    checkSource(s, ids);
  }
  return {
    chains,
    sources,
    port: envInt("PORT", 8081),
    dbPath: process.env.DB_PATH?.trim() || "./data/aggregator.db",
    pollIntervalMs: envInt("POLL_INTERVAL_MS", 5_000),
    windowDays: envInt("WINDOW_DAYS", 7),
  };
}
