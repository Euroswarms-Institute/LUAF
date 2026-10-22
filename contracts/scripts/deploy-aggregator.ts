import { ethers, network } from "hardhat";
import { writeFileSync, mkdirSync } from "fs";
import { join } from "path";

import { deploymentsDir, forkFilename, isLocalNode } from "./lib/venture-stack";

// The shared channel: the syndicate registry and the router that pays into it.
//
// AggregatorRouter charges on volume routed through any pool, ours or anyone
// else's. SyndicateRegistry decides which launchpads are members, and the
// router pays a member a share of its fee on volume touching that member's
// coins — including volume the member never brought, because the terminal's
// own users trade whatever is listed. Deregistering a launchpad stops that
// stream for every coin it ever launched, which is the exit cost the tenant
// model previously only claimed to have.
//
//   RPC_URL=… CHAIN_ID=4663 PRIVATE_KEY=0x… \
//   TREASURY=0x… npx hardhat run scripts/deploy-aggregator.ts --network target
//
// Env:
//   TREASURY      where the router's own share of the fee goes (required)
//   ADMIN         registry admin: admits and removes launchpads (default: deployer)
//   FEE_BPS       router fee on routed volume, 0..100 (default 100 = 1%)
//   REBATE_BPS    share of that fee paid to a member launchpad (default 3000 = 30%)
//   POOL_MANAGER  Uniswap V4 PoolManager (required for the V4 path)
//   V3_ROUTER     Uniswap V3 SwapRouter, or zero to disable the V3 path
//   WETH          wrapped native (required)
const INFRA: Record<number, { poolManager: string; weth: string; v3Router: string }> = {
  4663: {
    poolManager: "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    weth: "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    v3Router: "0xCaf681a66D020601342297493863E78C959E5cb2",
  },
  46630: {
    poolManager: "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    weth: "0x33e4191705c386532ba27cBF171Db86919200B94",
    v3Router: ethers.ZeroAddress,
  },
};

async function main() {
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  // A fork reports the forked chain's id, so a deploy run against one would
  // otherwise write an artifact that looks like the real thing and names
  // addresses that exist nowhere. Mark it instead.
  const onFork = await isLocalNode();
  const infra = INFRA[chainId];
  const poolManager = process.env.POOL_MANAGER ?? infra?.poolManager;
  const weth = process.env.WETH ?? infra?.weth;
  const v3Router = process.env.V3_ROUTER ?? infra?.v3Router ?? ethers.ZeroAddress;
  if (!poolManager || !weth) throw new Error(`no infra for chain ${chainId}; set POOL_MANAGER and WETH`);

  const [signer] = await ethers.getSigners();
  const treasury = process.env.TREASURY;
  if (!treasury || !ethers.isAddress(treasury)) throw new Error("TREASURY must be an address");
  const admin = process.env.ADMIN ?? signer.address;
  const feeBps = Number(process.env.FEE_BPS ?? 100);
  const rebateBps = Number(process.env.REBATE_BPS ?? 3000);
  if (!Number.isInteger(feeBps) || feeBps < 0 || feeBps > 100) throw new Error("FEE_BPS must be 0..100");
  if (!Number.isInteger(rebateBps) || rebateBps < 0 || rebateBps > 10_000) throw new Error("REBATE_BPS must be 0..10000");

  console.log(`network: ${network.name} (${chainId})  deployer: ${signer.address}`);
  console.log(`treasury: ${treasury}  admin: ${admin}  feeBps: ${feeBps}  rebateBps: ${rebateBps}`);

  const registry = await (await ethers.getContractFactory("SyndicateRegistry")).deploy(admin);
  await registry.waitForDeployment();
  const router = await (await ethers.getContractFactory("AggregatorRouter")).deploy(
    poolManager, v3Router, weth, treasury, feeBps, await registry.getAddress(), rebateBps,
  );
  await router.waitForDeployment();
  const startBlock = await ethers.provider.getBlockNumber();

  const out = {
    chainId,
    admin,
    treasury,
    feeBps,
    rebateBps,
    startBlock,
    ...(onFork ? { fork: true, note: "deployed against a fork; these addresses are not real" } : {}),
    contracts: {
      registry: await registry.getAddress(),
      router: await router.getAddress(),
      poolManager,
      weth,
      v3Router,
    },
  };
  const dir = deploymentsDir();
  mkdirSync(dir, { recursive: true });
  const file = onFork ? forkFilename(`aggregator-${chainId}.json`) : `aggregator-${chainId}.json`;
  if (onFork) {
    console.log(`\n*** The RPC is a local development node, so this is a rehearsal. The`);
    console.log(`*** addresses above exist only on it. Saving as ${file}.`);
  }
  // join(dir, …), not join(__dirname, …): the second ignored DEPLOYMENTS_DIR
  // and wrote a rehearsal's addresses into the repository anyway.
  writeFileSync(join(dir, file), JSON.stringify(out, null, 2));
  console.log(`registry: ${out.contracts.registry}  router: ${out.contracts.router}`);
  console.log(`saved ${join(dir, file)}  startBlock: ${startBlock}`);
  console.log(`\nAdmit a launchpad with: registry.setFactory(<factory>, <beneficiary>)`);
}
main().catch((e) => { console.error(e); process.exit(1); });
