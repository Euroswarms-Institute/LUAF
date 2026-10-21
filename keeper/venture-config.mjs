// Shared, strict configuration for the venture keepers. Both scripts move
// treasury ETH, so a malformed setting stops the run before anything is sent
// instead of being read as zero. An empty variable counts as unset: GitHub
// Actions passes unset vars and secrets as "".
import { ethers } from "ethers";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));

// The stacks these keepers service are deployed by contracts/scripts, which
// write their artifacts to contracts/deployments. The path is still always
// given rather than guessed from a chain id: one chain can carry several tenant
// stacks, and a keeper pointed at the wrong one would sell the wrong treasury's
// coins. Naming a default here would only turn that into a silent mistake.
const DEPLOYMENTS = {};

function fail(msg) {
  console.error(msg);
  process.exit(1);
}

function raw(name) {
  const v = process.env[name]?.trim();
  return v === "" ? undefined : v;
}

/** An integer in [min, max], or the default when unset. */
export function envInt(name, fallback, min, max) {
  const v = raw(name);
  if (v === undefined) return fallback;
  if (!/^\d+$/.test(v) || Number(v) < min || Number(v) > max) fail(`${name}=${v}: expected an integer from ${min} to ${max}`);
  return Number(v);
}

/** A non-negative integer amount (wei or token units), or the default. */
export function envWei(name, fallback) {
  const v = raw(name);
  if (v === undefined) return fallback;
  if (!/^\d+$/.test(v)) fail(`${name}=${v}: expected a whole number`);
  return BigInt(v);
}

/** An ETH amount like "0.5", in wei, or the default. */
export function envEth(name, fallback) {
  const v = raw(name) ?? fallback;
  if (!/^\d+(\.\d{1,18})?$/.test(v)) fail(`${name}=${v}: expected an ETH amount like 0.5`);
  return ethers.parseEther(v);
}

export const DRY_RUN = raw("DRY_RUN") !== undefined;
export const RPC = raw("RPC_URL") ?? "https://rpc.testnet.chain.robinhood.com";
export const KEY = raw("KEEPER_PRIVATE_KEY") ?? fail("Set KEEPER_PRIVATE_KEY.");
export const LOG_CHUNK = envInt("LOG_CHUNK", 500_000, 1, 10_000_000);

/** The deployment for CHAIN_ID (or DEPLOYMENT_FILE). The caller still checks
 *  it against the chain RPC_URL actually serves. */
export function loadDeployment() {
  const chainId = envInt("CHAIN_ID", 46630, 1, Number.MAX_SAFE_INTEGER);
  const explicit = raw("DEPLOYMENT_FILE");
  if (!explicit && !DEPLOYMENTS[chainId]) {
    fail(
      `Set DEPLOYMENT_FILE to the venture or tenant deployment JSON for chain ${chainId} ` +
        "(written by deploy-tenant.ts, e.g. " +
        "contracts/deployments/tenant-<slug>-<chainId>.json).",
    );
  }
  const path = explicit ?? join(here, "../contracts/deployments", DEPLOYMENTS[chainId]);
  let dep;
  try { dep = JSON.parse(readFileSync(path, "utf8")); } catch (e) { fail(`can't read ${path}: ${e.message}`); }
  if (!dep?.contracts || (!explicit && Number(dep.chainId) !== chainId)) fail(`${path} is not a deployment for chain ${chainId}`);
  return { dep, depPath: path };
}
