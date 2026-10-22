import { ethers, network } from "hardhat";
import { readFileSync } from "fs";
import { isAbsolute, resolve } from "path";

// Admit (or remove) a launchpad in the syndicate registry.
//
//   RPC_URL=… CHAIN_ID=… PRIVATE_KEY=<the registry admin's> \
//   AGGREGATOR_DEPLOYMENT=deployments/aggregator-4663.json \
//   FACTORY=0x… BENEFICIARY=0x… \
//   npx hardhat run scripts/registry-admit.ts --network target
//
// A registry nobody populates pays nobody, and the shared router reads
// membership live, so this is the step that turns routed volume into income for
// the stack that launched the coin -- including our own stacks, which is what
// makes the terminal's volume pay the factory underneath it.
//
// Set BENEFICIARY=0x0 to remove a launchpad. Removal stops the stream for every
// coin it ever launched, which is the exit cost the tenant model rests on.
//
// Only the registry's immutable admin can do either.
async function main() {
  const p = process.env.AGGREGATOR_DEPLOYMENT?.trim();
  if (!p) throw new Error("set AGGREGATOR_DEPLOYMENT");
  const dep = JSON.parse(readFileSync(isAbsolute(p) ? p : resolve(process.cwd(), p), "utf8"));
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  if (Number(dep.chainId) !== chainId) throw new Error(`artifact is chain ${dep.chainId}, the RPC is ${chainId}`);

  const factoryAddr = process.env.FACTORY?.trim();
  const beneficiary = process.env.BENEFICIARY?.trim();
  if (!factoryAddr || !ethers.isAddress(factoryAddr)) throw new Error("FACTORY must be an address");
  if (!beneficiary || !ethers.isAddress(beneficiary)) throw new Error("BENEFICIARY must be an address (0x0 to remove)");

  const [signer] = await ethers.getSigners();
  const registry = await ethers.getContractAt("SyndicateRegistry", dep.contracts.registry);
  const admin = await registry.admin();
  if (admin.toLowerCase() !== signer.address.toLowerCase()) {
    throw new Error(`the registry's admin is ${admin}, this key is ${signer.address}. It is immutable; there is no other way in.`);
  }
  console.log(`network: ${network.name} (${chainId})  registry: ${dep.contracts.registry}`);
  const before = await registry.beneficiaryOfFactory(factoryAddr);
  await (await registry.setFactory(factoryAddr, beneficiary)).wait();
  console.log(`${factoryAddr}: ${before} -> ${beneficiary}`);
  console.log(beneficiary === ethers.ZeroAddress
    ? "removed: routed volume on its coins now pays nobody"
    : "admitted: the shared router now pays this beneficiary on volume touching its coins");
}
main().catch((e) => { console.error(e); process.exit(1); });
