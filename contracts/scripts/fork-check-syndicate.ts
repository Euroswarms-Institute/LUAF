import { ethers, network } from "hardhat";
import { readFileSync } from "fs";
import { join } from "path";

// Proves the syndicate against the real venture stack, not a mock. The unit
// tests use a stub factory, so they cannot catch a mismatch with the deployed
// VentureFactory's listings ABI — the one integration risk in the registry.
// This admits the real factory, registers a coin it actually launched, reads
// the beneficiary back through it, and checks that removal stops the stream.
//
//   ROBINHOOD_RPC_URL=https://rpc.testnet.chain.robinhood.com \
//   ROBINHOOD_CHAIN_ID=46630 FORK_HARDFORK=cancun \
//   VENTURE_DEPLOYMENT=../DoublePlus/contracts/deployments/venture-testnet.json \
//   npm run fork:syndicate
//
// The deployment artifact is written by the venture deploy, which lives in the
// DoublePlus repo, so its path is given rather than assumed: this repo holds
// the registry, not the stack it is proved against.
//
// Needs no key and spends nothing: everything happens on a local fork.
async function main() {
  const file = process.env.VENTURE_DEPLOYMENT?.trim();
  if (!file) {
    throw new Error(
      "set VENTURE_DEPLOYMENT to a venture deployment JSON (e.g. a DoublePlus " +
        "contracts/deployments/venture-<name>.json): this fork check proves the registry " +
        "against a stack that is already deployed",
    );
  }
  const dep = JSON.parse(readFileSync(file.startsWith("/") ? file : join(process.cwd(), file), "utf8"));
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  if (chainId !== Number(dep.chainId)) throw new Error(`fork is chain ${chainId}, deployment is ${dep.chainId}`);
  const [signer] = await ethers.getSigners();
  const tenant = "0x1B97531b6Df3E96ab6899F880db7B11F2f45Acce";

  const registry = await (await ethers.getContractFactory("SyndicateRegistry")).deploy(signer.address);
  await registry.waitForDeployment();
  console.log(`registry deployed on the fork: ${await registry.getAddress()}`);

  const factory = await ethers.getContractAt(
    ["function totalTokens() view returns (uint256)", "function allTokens(uint256) view returns (address)",
     "function listings(address) view returns (address creator, address pair, uint16 taxBps, uint64 createdAt, bytes32 poolId)"],
    dep.contracts.factory,
  );
  const total = Number(await factory.totalTokens());
  console.log(`real factory ${dep.contracts.factory} has ${total} coin(s)`);
  if (total === 0) throw new Error("no coins on the testnet factory to test with");

  // Before membership, registration must be refused.
  const coin = await factory.allTokens(0);
  const l = await factory.listings(coin);
  console.log(`coin ${coin}  creator ${l.creator}  graduated ${l.poolId !== ethers.ZeroHash}`);
  try {
    await registry.registerCoin(dep.contracts.factory, coin);
    throw new Error("FAIL: a non-member factory was allowed to register");
  } catch (e) {
    if (String(e).includes("FAIL:")) throw e;
    console.log("non-member registration refused, as it should be");
  }

  await (await registry.setFactory(dep.contracts.factory, tenant)).wait();
  await (await registry.registerCoin(dep.contracts.factory, coin)).wait();
  const who = await registry.beneficiaryFor(coin);
  if (who.toLowerCase() !== tenant.toLowerCase()) throw new Error(`FAIL: beneficiary is ${who}, wanted ${tenant}`);
  console.log(`registered against the real factory; beneficiaryFor(${coin.slice(0, 10)}...) = ${who}`);

  // A coin that factory never launched must be refused.
  try {
    await registry.registerCoin(dep.contracts.factory, dep.contracts.weth);
    throw new Error("FAIL: a foreign address was accepted as a coin");
  } catch (e) {
    if (String(e).includes("FAIL:")) throw e;
    console.log("a coin the factory never launched was refused");
  }

  // Leaving stops the stream for a coin already registered.
  await (await registry.setFactory(dep.contracts.factory, ethers.ZeroAddress)).wait();
  const after = await registry.beneficiaryFor(coin);
  if (after !== ethers.ZeroAddress) throw new Error(`FAIL: still paying ${after} after removal`);
  console.log("after removal the same coin pays nobody: the exit cost holds against the real factory");
  console.log(`\nall checks passed on a fork of ${network.name} chain ${chainId}`);
}
main().catch((e) => { console.error(e); process.exit(1); });
