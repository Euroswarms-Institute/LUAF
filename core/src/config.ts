/** Configuration and the deployment artifacts the pipeline launches into.
 *
 *  Every setting comes from the environment, and a malformed one stops the run
 *  before anything is signed rather than being read as zero. This mirrors
 *  DoublePlus's `keeper/venture-config.mjs`: an empty variable counts as unset,
 *  because CI passes unset secrets as "".
 *
 *  The deployment JSONs are the authority on addresses, never a .env file. A
 *  `launcher-<tenant>-<chainId>.json` is written by the Launcher deploy script
 *  and carries both our Launcher and the tenant's venture stack. */

import { readFileSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import { isAddress, type Address, type Hex } from "viem";

export class ConfigError extends Error {}

function fail(msg: string): never {
  throw new ConfigError(msg);
}

/** An env var, with "" treated as unset. */
export function raw(name: string): string | undefined {
  const v = process.env[name]?.trim();
  return v === "" ? undefined : v;
}

export function envStr(name: string, fallback?: string): string {
  const v = raw(name) ?? fallback;
  if (v === undefined) fail(`set ${name}`);
  return v;
}

export function envInt(name: string, fallback: number, min: number, max: number): number {
  const v = raw(name);
  if (v === undefined) return fallback;
  if (!/^\d+$/.test(v) || Number(v) < min || Number(v) > max) {
    fail(`${name}=${v}: expected an integer from ${min} to ${max}`);
  }
  return Number(v);
}

export function envBool(name: string, fallback: boolean): boolean {
  const v = raw(name)?.toLowerCase();
  if (v === undefined) return fallback;
  if (["1", "true", "yes", "on"].includes(v)) return true;
  if (["0", "false", "no", "off"].includes(v)) return false;
  fail(`${name}=${v}: expected a boolean`);
}

/** An ETH amount like "0.05", in wei. */
export function envEth(name: string, fallback: string): bigint {
  const v = raw(name) ?? fallback;
  if (!/^\d+(\.\d{1,18})?$/.test(v)) fail(`${name}=${v}: expected an ETH amount like 0.05`);
  const [whole = "0", frac = ""] = v.split(".");
  return BigInt(whole) * 10n ** 18n + BigInt(frac.padEnd(18, "0"));
}

export function envAddress(name: string, fallback?: string): Address {
  const v = raw(name) ?? fallback;
  if (!v || !isAddress(v)) fail(`${name} must be an address`);
  return v as Address;
}

/** The slice of a `launcher-<tenant>-<chain>.json` the pipeline reads. */
export interface LauncherDeployment {
  chainId: number;
  tenant: string;
  operator: Address;
  startBlock: number;
  /** The address mark every coin carries, e.g. "0x2add". Mined with CREATE2. */
  vanity?: Hex;
  contracts: {
    launcher: Address;
    creatorSplitter: Address;
    factory: Address;
    hook: Address;
    router: Address;
    tokenDeployer: Address;
    poolManager: Address;
    weth: Address;
    platformSplitter: Address;
  };
}

const REQUIRED_CONTRACTS = [
  "launcher",
  "creatorSplitter",
  "factory",
  "hook",
  "tokenDeployer",
  "weth",
] as const;

/** Where deployments live when no explicit path is given. */
export function deploymentsDir(): string {
  const d = raw("LUCRE_DEPLOYMENTS_DIR");
  if (d) return isAbsolute(d) ? d : resolve(process.cwd(), d);
  return resolve(process.cwd(), "contracts/deployments");
}

/** Load the Launcher deployment for a tenant, by explicit path or by slug. */
export function loadLauncherDeployment(opts: { tenant?: string; chainId?: number; file?: string } = {}): LauncherDeployment {
  const file = opts.file ?? raw("LUCRE_LAUNCHER_DEPLOYMENT");
  const tenant = opts.tenant ?? raw("LUCRE_TENANT");
  const chainId = opts.chainId ?? envInt("LUCRE_CHAIN_ID", 46630, 1, Number.MAX_SAFE_INTEGER);
  let path: string;
  if (file) {
    path = isAbsolute(file) ? file : resolve(process.cwd(), file);
  } else {
    if (!tenant) fail("set LUCRE_TENANT (or LUCRE_LAUNCHER_DEPLOYMENT)");
    path = join(deploymentsDir(), `launcher-${tenant}-${chainId}.json`);
  }
  let dep: LauncherDeployment;
  try {
    dep = JSON.parse(readFileSync(path, "utf8")) as LauncherDeployment;
  } catch (e) {
    fail(`can't read ${path}: ${(e as Error).message}`);
  }
  if (!dep.contracts) fail(`${path} is not a launcher deployment`);
  for (const k of REQUIRED_CONTRACTS) {
    const v = dep.contracts[k];
    if (!v || !isAddress(v)) fail(`${path}: contracts.${k} is missing or not an address`);
  }
  if (!file && Number(dep.chainId) !== chainId) {
    fail(`${path} is for chain ${dep.chainId}, not ${chainId}`);
  }
  return dep;
}

export interface LlmConfig {
  apiKey: string;
  baseUrl: string;
  model: string;
  temperature: number;
  maxTokens: number;
  timeoutMs: number;
}

/** OpenAI-compatible endpoint settings. No key is ever defaulted in source. */
export function llmConfig(): LlmConfig {
  return {
    apiKey: envStr("OPENAI_API_KEY"),
    baseUrl: envStr("OPENAI_BASE_URL", "https://api.openai.com/v1").replace(/\/+$/, ""),
    model: envStr("LUCRE_MODEL", "gpt-5.1"),
    temperature: Number(raw("LUCRE_TEMPERATURE") ?? "0.9"),
    maxTokens: envInt("LUCRE_MAX_TOKENS", 32_000, 256, 400_000),
    timeoutMs: envInt("LUCRE_LLM_TIMEOUT_MS", 1_200_000, 1_000, 3_600_000),
  };
}

export interface RunConfig {
  /** Nothing is signed when true: the pipeline designs, validates and reports. */
  dryRun: boolean;
  rpcUrl: string;
  /** Operator key. Only read when dryRun is false. */
  privateKey?: Hex;
  /** Attempts at design + validation before a run is abandoned. */
  maxSteps: number;
  /** Minimum substantive lines of generated code. */
  minLines: number;
  validationTimeoutMs: number;
  /** Where generated agents and bundles are written. */
  outDir: string;
}

export function runConfig(): RunConfig {
  const dryRun = envBool("LUCRE_DRY_RUN", true);
  const key = raw("LUCRE_PRIVATE_KEY");
  if (!dryRun && !key) fail("set LUCRE_PRIVATE_KEY, or leave LUCRE_DRY_RUN=1");
  if (key && !/^0x[0-9a-fA-F]{64}$/.test(key)) fail("LUCRE_PRIVATE_KEY must be a 0x-prefixed 32-byte hex key");
  return {
    dryRun,
    rpcUrl: envStr("LUCRE_RPC_URL", "https://rpc.testnet.chain.robinhood.com"),
    privateKey: key as Hex | undefined,
    maxSteps: envInt("LUCRE_MAX_STEPS", 3, 1, 20),
    minLines: envInt("LUCRE_MIN_LINES", 400, 1, 100_000),
    validationTimeoutMs: envInt("LUCRE_VALIDATION_TIMEOUT_MS", 600_000, 1_000, 3_600_000),
    outDir: envStr("LUCRE_OUT_DIR", "runs"),
  };
}
