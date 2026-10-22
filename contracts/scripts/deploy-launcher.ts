import { ethers, network } from "hardhat";
import { readFileSync, writeFileSync, mkdirSync } from "fs";
import { join } from "path";
import { deploymentsDir, forkFilename, isLocalNode } from "./lib/venture-stack";

// Deploy the pipeline's Launcher for one tenant: a creator FeeSplitter
// (tenant / protocol, PROTOCOL_BPS) and a Launcher that names it as the
// destination of every coin's creator income.
//
//   RPC_URL=… CHAIN_ID=4663 PRIVATE_KEY=0x… \
//   TENANT_DEPLOYMENT=deployments/tenant-acme-4663.json \
//   OPERATOR=0x… npx hardhat run scripts/deploy-launcher.ts --network target
//
// Env:
//   TENANT_DEPLOYMENT  the tenant deployment JSON written by deploy-tenant.ts
//   OPERATOR           the pipeline's hot wallet, the only address that may launch
//   PROTOCOL_BPS       protocol share of creator income (default 1000 = 10%)
//   TENANT_TREASURY / PROTOCOL_TREASURY  override the tenant JSON's recipients
async function main() {
  const depPath = process.env.TENANT_DEPLOYMENT;
  if (!depPath) throw new Error("set TENANT_DEPLOYMENT");
  const dep = JSON.parse(readFileSync(depPath, "utf8"));
  if (!dep.tenant?.slug || !dep.contracts?.factory) throw new Error(`${depPath} is not a tenant deployment`);
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  if (Number(dep.chainId) !== chainId) throw new Error(`${depPath} is for chain ${dep.chainId}, network is ${chainId}`);

  const operator = process.env.OPERATOR;
  if (!operator || !ethers.isAddress(operator)) throw new Error("OPERATOR must be an address");
  const tenant = process.env.TENANT_TREASURY ?? dep.tenant.treasury;
  const protocol = process.env.PROTOCOL_TREASURY ?? dep.tenant.protocolTreasury;
  const protocolBps = Number(process.env.PROTOCOL_BPS ?? 1000);
  if (!Number.isInteger(protocolBps) || protocolBps < 0 || protocolBps > 10_000) throw new Error("PROTOCOL_BPS out of range");

  console.log(`network: ${network.name} (${chainId})  tenant: ${dep.tenant.slug}  operator: ${operator}`);
  const splitter = await (await ethers.getContractFactory("FeeSplitter")).deploy(tenant, protocol, protocolBps, dep.contracts.poolManager);
  await splitter.waitForDeployment();
  const launcher = await (await ethers.getContractFactory("Launcher")).deploy(operator, await splitter.getAddress());
  await launcher.waitForDeployment();
  const startBlock = await ethers.provider.getBlockNumber();
  // The pipeline mines a CREATE2 salt for every launch, which needs the exact
  // QuiverToken creation bytecode the deployed token deployer uses. Recording
  // it here means a release never has to carry build artifacts, and can never
  // mine against a bytecode the chain does not agree with.
  const tokenBytecode = (await ethers.getContractFactory("QuiverToken")).bytecode;

  const onFork = await isLocalNode();
  const out = {
    chainId,
    ...(onFork ? { fork: true, note: "deployed against a local node; these addresses are not real" } : {}),
    tenant: dep.tenant.slug,
    tenantDeployment: depPath,
    operator,
    creatorSplit: { tenant, protocol, protocolBps },
    startBlock,
    contracts: {
      launcher: await launcher.getAddress(),
      creatorSplitter: await splitter.getAddress(),
      factory: dep.contracts.factory,
      hook: dep.contracts.hook,
      router: dep.contracts.router,
      tokenDeployer: dep.contracts.tokenDeployer,
      poolManager: dep.contracts.poolManager,
      weth: dep.contracts.weth,
      platformSplitter: dep.tenant.splitter,
    },
    vanity: dep.vanity,
    tokenBytecode,
  };
  const dir = deploymentsDir();
  mkdirSync(dir, { recursive: true });
  const base = `launcher-${dep.tenant.slug}-${chainId}.json`;
  const file = onFork ? forkFilename(base) : base;
  writeFileSync(join(dir, file), JSON.stringify(out, null, 2));
  console.log(`launcher: ${out.contracts.launcher}  creatorSplitter: ${out.contracts.creatorSplitter}`);
  console.log(`saved ${join(dir, file)}`);
}
main().catch((e) => { console.error(e); process.exit(1); });
