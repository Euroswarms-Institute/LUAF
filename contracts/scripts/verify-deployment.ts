import { ethers, network } from "hardhat";
import { readFileSync } from "fs";
import { isAbsolute, resolve } from "path";

import { isLocalNode } from "./lib/venture-stack";

// Reads a deployment back off the chain and checks every wire in it.
//
//   RPC_URL=… CHAIN_ID=4663 \
//   TENANT_DEPLOYMENT=deployments/tenant-acme-4663.json \
//   LAUNCHER_DEPLOYMENT=deployments/launcher-acme-4663.json \
//   AGGREGATOR_DEPLOYMENT=deployments/aggregator-4663.json \
//   npx hardhat run scripts/verify-deployment.ts --network target
//
// Every address in the stack is an immutable constructor argument of something
// else: the hook names the treasury and the factory, the factory names the hook
// and the token deployer, the router names the factory, the splitter names its
// recipients, the Launcher names the splitter. The deploy scripts pass them
// correctly or the stack is permanently misrouted, and nothing about a
// successful deploy transaction says which. This asks the contracts.
//
// Pure reads: no key, no gas, safe against mainnet. Run it immediately after a
// deploy, and again whenever an artifact is picked up by something new.
//
// At least one of the three artifacts is required; each one given is checked.
let failures = 0;
function eq(label: string, got: unknown, want: unknown) {
  const g = typeof got === "string" && ethers.isAddress(got) ? ethers.getAddress(got) : String(got);
  const w = typeof want === "string" && ethers.isAddress(want) ? ethers.getAddress(want) : String(want);
  if (g === w) console.log(`  ok    ${label} = ${g}`);
  else { failures++; console.log(`  FAIL  ${label} is ${g}, artifact says ${w}`); }
}
function note(label: string, value: unknown) { console.log(`  --    ${label} = ${value}`); }

function load(name: string) {
  const p = process.env[name]?.trim();
  if (!p) return undefined;
  const abs = isAbsolute(p) ? p : resolve(process.cwd(), p);
  const json = JSON.parse(readFileSync(abs, "utf8"));
  if (json.fork) console.log(`(${name} is marked fork: true — its addresses are a rehearsal's)`);
  return json;
}

async function hasCode(label: string, addr: string): Promise<boolean> {
  const code = await ethers.provider.getCode(addr);
  if (code === "0x") { failures++; console.log(`  FAIL  ${label} ${addr} has no code`); return false; }
  return true;
}

const HOOK_ABI = [
  "function poolManager() view returns (address)",
  "function platformTreasury() view returns (address)",
  "function launcher() view returns (address)",
  "function platformFeeBps() view returns (uint16)",
  "function refShareBps() view returns (uint16)",
];
const FACTORY_ABI = [
  "function poolManager() view returns (address)",
  "function hook() view returns (address)",
  "function weth() view returns (address)",
  "function v3Router() view returns (address)",
  "function tokenDeployer() view returns (address)",
  "function vestingDeployer() view returns (address)",
  "function protocolAdmin() view returns (address)",
  "function minTargetWei() view returns (uint256)",
  "function curveBuyFeeBps() view returns (uint16)",
  "function curveSellFeeBps() view returns (uint16)",
  "function vanity() view returns (uint16)",
  "function owner() view returns (address)",
  "function totalTokens() view returns (uint256)",
];
const ROUTER_ABI = [
  "function poolManager() view returns (address)",
  "function factory() view returns (address)",
  "function hook() view returns (address)",
  "function weth() view returns (address)",
];
const SPLITTER_ABI = [
  "function tenant() view returns (address)",
  "function protocol() view returns (address)",
  "function protocolBps() view returns (uint16)",
  "function poolManager() view returns (address)",
];
const LAUNCHER_ABI = [
  "function operator() view returns (address)",
  "function splitter() view returns (address)",
];
const AGG_ROUTER_ABI = [
  "function poolManager() view returns (address)",
  "function v3Router() view returns (address)",
  "function weth() view returns (address)",
  "function treasury() view returns (address)",
  "function feeBps() view returns (uint16)",
  "function registry() view returns (address)",
  "function rebateBps() view returns (uint16)",
];

async function verifyTenant(dep: any) {
  console.log("\ntenant stack");
  const c = dep.contracts;
  for (const [k, v] of Object.entries(c) as [string, string][]) {
    if (v === ethers.ZeroAddress) continue;
    await hasCode(k, v);
  }

  const hook = await ethers.getContractAt(HOOK_ABI, c.hook);
  eq("hook.poolManager", await hook.poolManager(), c.poolManager);
  // The one that cannot be fixed: every trade fee on every graduated pool from
  // this stack goes here, for as long as the chain exists.
  eq("hook.platformTreasury", await hook.platformTreasury(), dep.treasury);
  eq("hook.launcher", await hook.launcher(), c.factory);
  eq("hook.platformFeeBps", Number(await hook.platformFeeBps()), dep.platformFeeBps);
  eq("hook.refShareBps", Number(await hook.refShareBps()), dep.refShareBps);

  const factory = await ethers.getContractAt(FACTORY_ABI, c.factory);
  eq("factory.hook", await factory.hook(), c.hook);
  eq("factory.poolManager", await factory.poolManager(), c.poolManager);
  eq("factory.weth", await factory.weth(), c.weth);
  eq("factory.v3Router", await factory.v3Router(), c.v3Router);
  eq("factory.tokenDeployer", await factory.tokenDeployer(), c.tokenDeployer);
  eq("factory.vestingDeployer", await factory.vestingDeployer(), c.vestingDeployer);
  eq("factory.protocolAdmin", await factory.protocolAdmin(), dep.admin);
  eq("factory.minTargetWei", (await factory.minTargetWei()).toString(), dep.minTargetWei);
  eq("factory.curveBuyFeeBps", Number(await factory.curveBuyFeeBps()), dep.curveBuyFeeBps);
  eq("factory.curveSellFeeBps", Number(await factory.curveSellFeeBps()), dep.curveSellFeeBps);
  eq("factory.vanity", `0x${Number(await factory.vanity()).toString(16)}`, dep.vanity);
  // The deploy renounces ownership as its last step. An owned factory means the
  // deploy stopped early and its setParams values may not be the artifact's.
  eq("factory.owner (renounced)", await factory.owner(), ethers.ZeroAddress);
  note("factory.totalTokens", (await factory.totalTokens()).toString());

  const router = await ethers.getContractAt(ROUTER_ABI, c.router);
  eq("router.factory", await router.factory(), c.factory);
  eq("router.hook", await router.hook(), c.hook);
  eq("router.weth", await router.weth(), c.weth);

  if (dep.tenant?.splitter) {
    const s = await ethers.getContractAt(SPLITTER_ABI, dep.tenant.splitter);
    eq("platform splitter.tenant", await s.tenant(), dep.tenant.treasury);
    eq("platform splitter.protocol", await s.protocol(), dep.tenant.protocolTreasury);
    eq("platform splitter.protocolBps", Number(await s.protocolBps()), dep.tenant.protocolBps);
    eq("platform splitter.poolManager", await s.poolManager(), c.poolManager);
    // The hook pays the platform treasury; if that is not this splitter, the
    // tenant's cut never reaches the tenant.
    eq("hook pays the platform splitter", await hook.platformTreasury(), dep.tenant.splitter);
  }
}

async function verifyLauncher(dep: any) {
  console.log("\nlauncher");
  const c = dep.contracts;
  await hasCode("launcher", c.launcher);
  await hasCode("creatorSplitter", c.creatorSplitter);
  const l = await ethers.getContractAt(LAUNCHER_ABI, c.launcher);
  eq("launcher.operator", await l.operator(), dep.operator);
  eq("launcher.splitter", await l.splitter(), c.creatorSplitter);
  const s = await ethers.getContractAt(SPLITTER_ABI, c.creatorSplitter);
  eq("creator splitter.tenant", await s.tenant(), dep.creatorSplit.tenant);
  eq("creator splitter.protocol", await s.protocol(), dep.creatorSplit.protocol);
  eq("creator splitter.protocolBps", Number(await s.protocolBps()), dep.creatorSplit.protocolBps);
  // The pipeline mines a CREATE2 salt against this bytecode. If it is not the
  // bytecode the deployed token deployer uses, every mined salt is wrong and
  // every launch reverts on the vanity check.
  const local = (await ethers.getContractFactory("QuiverToken")).bytecode;
  eq("recorded QuiverToken bytecode matches this build", ethers.keccak256(dep.tokenBytecode), ethers.keccak256(local));
}

async function verifyAggregator(dep: any) {
  console.log("\naggregator");
  const c = dep.contracts;
  await hasCode("registry", c.registry);
  await hasCode("router", c.router);
  const reg = await ethers.getContractAt(["function admin() view returns (address)"], c.registry);
  // Immutable: the only address that can ever admit or remove a launchpad.
  eq("registry.admin", await reg.admin(), dep.admin);
  const r = await ethers.getContractAt(AGG_ROUTER_ABI, c.router);
  eq("aggregator router.registry", await r.registry(), c.registry);
  eq("aggregator router.treasury", await r.treasury(), dep.treasury);
  eq("aggregator router.feeBps", Number(await r.feeBps()), dep.feeBps);
  eq("aggregator router.rebateBps", Number(await r.rebateBps()), dep.rebateBps);
  eq("aggregator router.poolManager", await r.poolManager(), c.poolManager);
  eq("aggregator router.weth", await r.weth(), c.weth);
  eq("aggregator router.v3Router", await r.v3Router(), c.v3Router);
}

async function main() {
  const tenant = load("TENANT_DEPLOYMENT");
  const launcher = load("LAUNCHER_DEPLOYMENT");
  const aggregator = load("AGGREGATOR_DEPLOYMENT");
  if (!tenant && !launcher && !aggregator) {
    throw new Error("set at least one of TENANT_DEPLOYMENT, LAUNCHER_DEPLOYMENT, AGGREGATOR_DEPLOYMENT");
  }
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  console.log(`network: ${network.name} (${chainId})${(await isLocalNode()) ? "  (local development node)" : ""}`);
  for (const [name, dep] of [["TENANT_DEPLOYMENT", tenant], ["LAUNCHER_DEPLOYMENT", launcher], ["AGGREGATOR_DEPLOYMENT", aggregator]] as const) {
    if (dep && Number(dep.chainId) !== chainId) throw new Error(`${name} is for chain ${dep.chainId}, the RPC is ${chainId}`);
  }

  if (tenant) await verifyTenant(tenant);
  if (launcher) await verifyLauncher(launcher);
  if (aggregator) await verifyAggregator(aggregator);

  console.log("");
  if (failures) { console.log(`${failures} wire(s) wrong.`); process.exit(1); }
  console.log("every wire matches the artifact.");
}
main().catch((e) => { console.error(e); process.exit(1); });
