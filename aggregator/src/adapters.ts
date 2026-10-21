/** Reading a launch out of a chain, whoever's launchpad it is.
 *
 *  An adapter turns logs into coin rows. Two exist, and the split is
 *  deliberate: our own launchpads are read against a known ABI, and everyone
 *  else's is read from a description supplied in configuration. Nothing in
 *  here hard-codes a third-party ABI, because an ABI we have not verified
 *  against the deployed contract is a silent source of wrong data — it would
 *  either yield nothing or, worse, decode the wrong argument as the coin. */

import { parseAbi, parseAbiItem, type AbiEvent, type Address, type Log, type PublicClient } from "viem";

import type { SourceConfig } from "./config.js";

export interface LaunchRecord {
  address: string;
  creator: string;
  name: string;
  symbol: string;
  block: number;
  /** What the curve has to take before the coin reaches an open market, in wei
   *  as a decimal string. Present when the launch event carries it. */
  targetRaiseWei?: string;
  /** Unix seconds by which it has to get there, if the launchpad sets one. */
  deadline?: number;
}

export interface GraduationRecord {
  address: string;
  poolId: string | null;
  block: number;
  /** What it actually took, in wei as a decimal string. */
  raisedWei?: string;
  /** What went into the pool, which is what the locked liquidity is. */
  pairSeededWei?: string;
}

export interface Adapter {
  /** Events to scan for new coins. */
  launchEvent: AbiEvent;
  /** Events that mean a coin reached an open market, if the source has one. */
  graduateEvent?: AbiEvent;
  decodeLaunch(log: Log): LaunchRecord | undefined;
  decodeGraduation(log: Log): GraduationRecord | undefined;
}

/** Our own venture factories. `Launched` carries the creator and the pair;
 *  the name and symbol are read from the token itself, because the factory
 *  does not put them in the event. */
const VENTURE_ABI = parseAbi([
  "event Launched(address indexed token, address indexed creator, address indexed pair, uint16 taxBps, uint256 targetRaiseWei, uint64 deadline, address vesting)",
  "event Graduated(address indexed token, bytes32 poolId, uint256 raisedWei, uint256 founderCutWei, uint256 pairSeeded)",
]);

function abiEvent(name: string): AbiEvent {
  const e = VENTURE_ABI.find((a) => a.type === "event" && a.name === name);
  if (!e) throw new Error(`no ${name} in the venture ABI`);
  return e as AbiEvent;
}

type Decoded = Log & { args?: Record<string, unknown>; eventName?: string };

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

/** A uint from an event, as a decimal string. Undefined rather than "0" when
 *  absent: a coin whose launchpad does not publish a target has no target, and
 *  that is not the same as a target of nothing. */
function uint(v: unknown): string | undefined {
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "number" && Number.isFinite(v)) return String(Math.trunc(v));
  if (typeof v === "string" && /^\d+$/.test(v)) return v;
  return undefined;
}

function secs(v: unknown): number | undefined {
  const n = uint(v);
  if (n === undefined) return undefined;
  const asNumber = Number(n);
  return Number.isSafeInteger(asNumber) && asNumber > 0 ? asNumber : undefined;
}

export function ventureAdapter(): Adapter {
  const launchEvent = abiEvent("Launched");
  const graduateEvent = abiEvent("Graduated");
  return {
    launchEvent,
    graduateEvent,
    decodeLaunch(log) {
      const args = (log as Decoded).args;
      const token = args?.token;
      if (typeof token !== "string") return undefined;
      return {
        address: token,
        creator: str(args?.creator),
        // Filled in by the indexer from the token contract.
        name: "",
        symbol: "",
        block: Number(log.blockNumber ?? 0n),
        targetRaiseWei: uint(args?.targetRaiseWei),
        deadline: secs(args?.deadline),
      };
    },
    decodeGraduation(log) {
      const args = (log as Decoded).args;
      const token = args?.token;
      if (typeof token !== "string") return undefined;
      return {
        address: token,
        poolId: str(args?.poolId) || null,
        block: Number(log.blockNumber ?? 0n),
        raisedWei: uint(args?.raisedWei),
        pairSeededWei: uint(args?.pairSeeded),
      };
    },
  };
}

/** Anyone else's launchpad, described in configuration. */
export function genericAdapter(s: SourceConfig): Adapter {
  if (!s.launchEvent || !s.map?.token) {
    throw new Error(`source ${s.id}: a generic adapter needs launchEvent and map.token`);
  }
  const launchEvent = parseAbiItem(s.launchEvent) as AbiEvent;
  if (launchEvent.type !== "event") throw new Error(`source ${s.id}: launchEvent is not an event`);
  const names = new Set(launchEvent.inputs.map((i) => i.name).filter(Boolean) as string[]);
  for (const [field, key] of Object.entries(s.map)) {
    if (key && !names.has(key)) {
      throw new Error(`source ${s.id}: map.${field} names ${JSON.stringify(key)}, which ${s.launchEvent} does not have`);
    }
  }
  let graduateEvent: AbiEvent | undefined;
  if (s.graduateEvent) {
    graduateEvent = parseAbiItem(s.graduateEvent) as AbiEvent;
    if (graduateEvent.type !== "event") throw new Error(`source ${s.id}: graduateEvent is not an event`);
    const gNames = new Set(graduateEvent.inputs.map((i) => i.name).filter(Boolean) as string[]);
    const key = s.graduateMap?.token;
    if (!key || !gNames.has(key)) {
      throw new Error(`source ${s.id}: graduateEvent needs graduateMap.token naming one of its arguments`);
    }
  }
  const map = s.map;
  return {
    launchEvent,
    graduateEvent,
    decodeLaunch(log) {
      const args = (log as Decoded).args;
      const token = args?.[map.token];
      // A signature that matches by topic but carries a different shape is
      // skipped rather than guessed at.
      if (typeof token !== "string" || !token.startsWith("0x") || token.length !== 42) return undefined;
      return {
        address: token,
        creator: map.creator ? str(args?.[map.creator]) : "",
        name: map.name ? str(args?.[map.name]) : "",
        symbol: map.symbol ? str(args?.[map.symbol]) : "",
        block: Number(log.blockNumber ?? 0n),
        targetRaiseWei: map.targetRaise ? uint(args?.[map.targetRaise]) : undefined,
        deadline: map.deadline ? secs(args?.[map.deadline]) : undefined,
      };
    },
    decodeGraduation(log) {
      const key = s.graduateMap?.token;
      if (!key) return undefined;
      const args = (log as Decoded).args;
      const token = args?.[key];
      if (typeof token !== "string") return undefined;
      return {
        address: token,
        poolId: null,
        block: Number(log.blockNumber ?? 0n),
        raisedWei: s.graduateMap?.raised ? uint(args?.[s.graduateMap.raised]) : undefined,
      };
    },
  };
}

export function adapterFor(s: SourceConfig): Adapter {
  return s.kind === "venture" ? ventureAdapter() : genericAdapter(s);
}

const ERC20_ABI = parseAbi([
  "function name() view returns (string)",
  "function symbol() view returns (string)",
]);

/** Name and symbol from the token itself, for sources whose event omits them.
 *  A token that does not answer is recorded without them rather than dropped:
 *  an unnamed coin is still inventory and still counts toward saturation. */
export async function readTokenMeta(
  client: PublicClient,
  address: Address,
): Promise<{ name: string; symbol: string }> {
  try {
    const [name, symbol] = await Promise.all([
      client.readContract({ address, abi: ERC20_ABI, functionName: "name" }),
      client.readContract({ address, abi: ERC20_ABI, functionName: "symbol" }),
    ]);
    return { name: String(name).slice(0, 128), symbol: String(symbol).slice(0, 32) };
  } catch {
    return { name: "", symbol: "" };
  }
}
