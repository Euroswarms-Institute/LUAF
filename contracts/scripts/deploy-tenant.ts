import { ethers } from "hardhat";
import { deployVentureStack, INFRA } from "./lib/venture-stack";

// Tenant launchpad deploy: a full venture stack for another brand, whose
// platformTreasury is a FeeSplitter. Every protocol fee the stack earns (curve
// fees, creation fees, swept escrow, the hook's per-swap fee) lands in the
// splitter and leaves PROTOCOL_BPS to the protocol, the rest to the tenant.
// The factory and hook are the unmodified venture contracts.
//
//   TENANT_SLUG=acme TENANT_TREASURY=0x... PROTOCOL_TREASURY=0x... \
//     npx hardhat run scripts/deploy-tenant.ts --network target
//
// Env (in addition to everything scripts/lib/venture-stack.ts reads):
//   TENANT_SLUG        brand id, [a-z0-9-]; names deployments/tenant-<slug>-<chainId>.json
//   TENANT_TREASURY    the tenant's fee recipient
//   PROTOCOL_TREASURY  the protocol's fee recipient
//   PROTOCOL_BPS       protocol cut of every fee, 0..10000 (default 1000 = 10%)
//
// ADMIN (pause/resume/setParams) stays with the protocol unless overridden:
// managed tenants do not get launch-parameter control.
async function main() {
  const slug = process.env.TENANT_SLUG ?? "";
  if (!/^[a-z0-9-]+$/.test(slug)) throw new Error("TENANT_SLUG must match [a-z0-9-]+");
  const tenant = process.env.TENANT_TREASURY;
  const protocol = process.env.PROTOCOL_TREASURY;
  if (!tenant || !ethers.isAddress(tenant)) throw new Error("TENANT_TREASURY must be an address");
  if (!protocol || !ethers.isAddress(protocol)) throw new Error("PROTOCOL_TREASURY must be an address");
  const protocolBps = Number(process.env.PROTOCOL_BPS ?? 1000);
  if (!Number.isInteger(protocolBps) || protocolBps < 0 || protocolBps > 10_000) throw new Error("PROTOCOL_BPS out of range");

  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  const infra = INFRA[chainId];
  if (!infra) throw new Error(`no venture infra config for chain ${chainId}`);

  const splitter = await (await ethers.getContractFactory("FeeSplitter")).deploy(
    tenant, protocol, protocolBps, infra.poolManager,
  );
  await splitter.waitForDeployment();
  const splitterAddr = await splitter.getAddress();
  console.log(`tenant: ${slug}  splitter: ${splitterAddr}  protocolBps: ${protocolBps}`);

  await deployVentureStack({
    treasury: splitterAddr,
    file: `tenant-${slug}-${chainId}.json`,
    extra: { tenant: { slug, treasury: tenant, protocolTreasury: protocol, protocolBps, splitter: splitterAddr } },
  });
}
main().catch((e) => { console.error(e); process.exit(1); });
