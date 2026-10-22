import { ethers } from "hardhat";
import { readFileSync } from "fs";

// What one launch actually costs the operator, against a stack that is already
// deployed.
//
//   FORK_URL=… FORK_CHAIN_ID=… npx hardhat node          # in one shell
//   RPC_URL=http://127.0.0.1:8545 CHAIN_ID=… \
//   TENANT_DEPLOYMENT=… LAUNCHER_DEPLOYMENT=… \
//   npx hardhat run scripts/measure-launch.ts --network target
//
// The number matters twice. It is what the operator wallet has to be funded
// with, and it is the floor the selection loop's reaper measures a launch
// format against: a format whose fees per copy fall below the cost of deploying
// another copy is not worth keeping. That floor was a guess until this ran.
//
// Impersonates the operator, so it is a fork-only measurement.

/** The factory accepts the vanity mark at either end of the address:
 *  `a & 0xffff == vanity || a >> 144 == vanity` (VentureFactory L505). Mining
 *  for the prefix alone is correct and takes about twice as many tries, which
 *  on this mark is twenty-odd seconds of keccak per coin instead of ten. */
function hasMark(addr: string, mark: bigint): boolean {
  const a = BigInt(addr);
  return (a >> 144n) === mark || (a & 0xffffn) === mark;
}

async function main() {
  const dep = JSON.parse(readFileSync(process.env.LAUNCHER_DEPLOYMENT!, "utf8"));
  const tenant = JSON.parse(readFileSync(process.env.TENANT_DEPLOYMENT!, "utf8"));
  const c = dep.contracts;
  // Open mode graduates on the protocol's threshold, so that -- not
  // minTargetWei -- is the number the curve and the launch-window cap are
  // sized from.
  const target = BigInt(process.env.GRADUATION_RAISE_WEI ?? tenant.minTargetWei);

  // The token's address is CREATE2 over its own constructor arguments, so a
  // second run with the same name lands on an address that already holds code
  // and reverts without a reason string -- which reads like a contract bug
  // rather than a repeat. The name carries a nonce so the script is re-runnable.
  const tag = process.env.LAUNCH_TAG ?? String(Date.now() % 1_000_000);
  const p = {
    name: `Smoke${tag}`, symbol: `SMK${tag}`, metadataURI: "", pair: c.weth,
    buyTaxBps: 300, sellTaxBps: 300,
    devWallet: c.creatorSplitter, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500,
    ethUsdPrice8: BigInt(process.env.ETH_USD_8 ?? 50_000_000_000_000n), targetRaiseWei: target, raiseDurationSecs: 0,
    // Open mode, which is what the pipeline launches in: the creator is paid out
    // of curve fees rather than a cut of the raise, so founderRaiseBps must be
    // zero and the graduation trigger is the protocol's, not the founder's.
    maxBuyWei: target, founderRaiseBps: 0, founderSupplyBps: 0, vestingSecs: 0,
    mode: 1, minHoldForDividends: 0, dividendMode: 0, v3Path: "0x",
  };

  // Mine the salt first, before any RPC call. The vanity mark lives in the
  // token's own address, so this is a keccak grind of tens of thousands of
  // tries and takes about twenty seconds. An RPC connection left idle that long
  // is closed by the node, and the next call then fails on a dead socket for
  // reasons that look nothing like the real cause. Anything that mines a salt
  // has to mine it before it opens a connection, not between two calls on one.
  const Token = await ethers.getContractFactory("QuiverToken");
  const encoded = ethers.AbiCoder.defaultAbiCoder().encode(
    ["string", "string", "string", "uint256", "address", "address", "uint16", "address", "uint256", "uint8"],
    [p.name, p.symbol, p.metadataURI, 10n ** 27n, c.launcher, c.factory, p.buyTaxBps, p.pair, 0n, p.dividendMode],
  );
  const hash = ethers.keccak256(ethers.concat([Token.bytecode, encoded]));
  const mark = BigInt(tenant.vanity);
  let salt = "", tries = 0;
  const t0 = Date.now();
  for (let i = 0n; i < 8_000_000n; i++) {
    tries++;
    const s = ethers.zeroPadValue(ethers.toBeHex(i), 32);
    if (hasMark(ethers.getCreate2Address(c.tokenDeployer, s, hash), mark)) { salt = s; break; }
  }
  if (!salt) throw new Error("no vanity salt");
  console.log(`salt: ${tries} tries, ${((Date.now() - t0) / 1000).toFixed(1)}s of keccak`);

  // The operator has to be a signer this process can sign for: `--network
  // target` holds PRIVATE_KEY locally and signs there, so impersonation on the
  // node is never consulted. Run this with PRIVATE_KEY set to the operator's
  // key -- on a fork that is one of the node's printed accounts.
  const [operator] = await ethers.getSigners();
  if (operator.address.toLowerCase() !== String(dep.operator).toLowerCase()) {
    throw new Error(`PRIVATE_KEY is ${operator.address}; this Launcher only accepts ${dep.operator}`);
  }
  // The compiled artifact, not a hand-written signature: LaunchParams has
  // twenty-two fields and a mistyped one (uint64 where the struct says uint256)
  // changes the selector, which arrives as "function selector was not
  // recognized" and looks like a wrong address.
  const launcher = await ethers.getContractAt("Launcher", c.launcher);

  // The Launcher buys inside the factory's 60-second launch window, where the
  // per-wallet cap is target/100. A first buy over that reverts the whole
  // launch, salt and all, so the pipeline's first buy is bounded by the
  // graduation target and not by what the operator feels like spending.
  const firstBuy = BigInt(process.env.FIRST_BUY_WEI ?? (target / 100n).toString());
  const r = await (await launcher.connect(operator).launch(c.factory, p, salt, ethers.id("spec-smoke"), 0, { value: firstBuy })).wait();
  console.log(`launch + first buy: ${r!.gasUsed.toString()} gas`);
  for (const [label, price] of [["testnet 46630", 10_000_000n], ["mainnet 4663", 21_318_000n]] as const) {
    console.log(`  ${label}: ${ethers.formatEther(r!.gasUsed * price)} ETH of gas, plus whatever the first buy is`);
  }
}
main().catch((e) => { console.error(e); process.exit(1); });
