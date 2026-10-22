import { ethers, network } from "hardhat";

import { INFRA, isLocalNode } from "./lib/venture-stack";

// Everything that has to be true before a deploy spends anything, checked in
// one read-only pass.
//
//   RPC_URL=… CHAIN_ID=4663 PRIVATE_KEY=0x… ADMIN=0x… TREASURY=0x… \
//   npx hardhat run scripts/preflight.ts --network target
//
// Why this exists rather than letting the deploy scripts find out: three of the
// choices a venture deploy makes can never be changed afterwards. The hook's
// platformTreasury, the FeeSplitter's recipients and split, and
// SyndicateRegistry's admin are all immutable, and the factory renounces
// ownership at the end of its own deploy. A typo in any of them is not a redeploy,
// it is a permanent loss of the income that stack will ever earn. The deploy also
// pays gas for a FeeSplitter and a mined hook before it reaches the factory, so a
// failure late in the sequence is a failure that already cost money.
//
// Exits non-zero on anything that would make the deploy wrong. Warnings print
// and do not stop it.
const ZERO = ethers.ZeroAddress;

let failures = 0;
let warnings = 0;
function fail(msg: string) { failures++; console.log(`  FAIL  ${msg}`); }
function warn(msg: string) { warnings++; console.log(`  WARN  ${msg}`); }
function ok(msg: string) { console.log(`  ok    ${msg}`); }

/** An address env var that must be set, must parse, and must not be zero. */
function required(name: string): string | undefined {
  const v = process.env[name]?.trim();
  if (!v) { fail(`${name} is not set`); return undefined; }
  if (!ethers.isAddress(v)) { fail(`${name}=${v} is not an address`); return undefined; }
  if (v === ZERO) { fail(`${name} is the zero address`); return undefined; }
  return ethers.getAddress(v);
}

function optional(name: string): string | undefined {
  const v = process.env[name]?.trim();
  if (!v) return undefined;
  if (!ethers.isAddress(v)) { fail(`${name}=${v} is not an address`); return undefined; }
  return ethers.getAddress(v);
}

async function main() {
  const declared = Number(process.env.CHAIN_ID ?? 0);
  // hardhat's own provider rejects the first request when CHAIN_ID disagrees
  // with the RPC, which is the right outcome reported in the wrong voice: a
  // stack trace instead of the one line this pass exists to print.
  let net;
  try {
    net = await ethers.provider.getNetwork();
  } catch (e: any) {
    const m = /chain id (\d+), but connected to a chain with id (\d+)/.exec(String(e?.message ?? e));
    if (!m) throw e;
    console.log("chain");
    fail(`CHAIN_ID=${m[1]} but the RPC answers ${m[2]}`);
    console.log(`\n1 failure(s). Do not deploy.`);
    process.exit(1);
  }
  const chainId = Number(net.chainId);
  const local = await isLocalNode();

  console.log(`network: ${network.name}  chainId: ${chainId}${local ? "  (local development node: a rehearsal)" : ""}`);

  console.log("\nchain");
  if (declared && declared !== chainId) {
    // The usual shape of this mistake is a mainnet RPC with a testnet CHAIN_ID
    // still in the shell, which deploys the whole stack to the wrong chain.
    fail(`CHAIN_ID=${declared} but the RPC answers ${chainId}`);
  } else if (!declared) {
    warn("CHAIN_ID is not set, so nothing cross-checks the RPC's answer");
  } else {
    ok(`CHAIN_ID matches the RPC (${chainId})`);
  }
  if (!INFRA[chainId] && !(process.env.POOL_MANAGER && process.env.WETH)) {
    fail(`chain ${chainId} has no entry in INFRA; set POOL_MANAGER and WETH`);
  }

  console.log("\ndeployer");
  const signers = await ethers.getSigners();
  if (signers.length === 0) {
    fail("no signer: PRIVATE_KEY is unset, so --network target has no account");
  } else {
    const signer = signers[0];
    const bal = await ethers.provider.getBalance(signer.address);
    ok(`${signer.address}`);
    // Priced from the chain rather than fixed in ETH. The three deploys were
    // measured on a fork of 46630 at 16,825,807 gas in total (tenant stack
    // 13,551,813 over ten transactions, launcher 1,493,277, aggregator
    // 1,780,717), so the floor is that times the live gas price, doubled for a
    // price move mid-deploy. A fixed 0.05 ETH, which is what this said before
    // the measurement, is about three hundred times the real cost and would
    // refuse a deploy that is funded many times over.
    const DEPLOY_GAS = 16_825_807n;
    const gasPrice = BigInt((await ethers.provider.getFeeData()).gasPrice ?? 0n);
    const floor = process.env.MIN_DEPLOY_BALANCE_ETH
      ? ethers.parseEther(process.env.MIN_DEPLOY_BALANCE_ETH)
      : DEPLOY_GAS * gasPrice * 2n;
    ok(`gas price ${ethers.formatUnits(gasPrice, "gwei")} gwei, so all three deploys cost about ${ethers.formatEther(DEPLOY_GAS * gasPrice)} ETH`);
    if (bal < floor) fail(`balance ${ethers.formatEther(bal)} ETH is under ${ethers.formatEther(floor)} ETH; the deploy can run out midway`);
    else ok(`balance ${ethers.formatEther(bal)} ETH`);
    const expected = optional("DEPLOYER");
    if (expected && expected !== signer.address) fail(`PRIVATE_KEY is ${signer.address}, DEPLOYER says ${expected}`);
    else if (expected) ok("PRIVATE_KEY matches DEPLOYER");
  }

  console.log("\ninfrastructure");
  const poolManager = process.env.POOL_MANAGER?.trim() || INFRA[chainId]?.poolManager;
  const weth = process.env.WETH?.trim() || INFRA[chainId]?.weth;
  const v3Router = process.env.V3_ROUTER?.trim() || INFRA[chainId]?.v3Router;
  for (const [name, addr] of [["PoolManager", poolManager], ["WETH", weth]] as const) {
    if (!addr) { fail(`${name} is unknown`); continue; }
    const code = await ethers.provider.getCode(addr);
    if (code === "0x") fail(`${name} ${addr} has no code on chain ${chainId}`);
    else ok(`${name} ${addr} (${(code.length - 2) / 2} bytes)`);
  }
  if (v3Router && v3Router !== ZERO) {
    const code = await ethers.provider.getCode(v3Router);
    if (code === "0x") fail(`V3 router ${v3Router} has no code; set V3_ROUTER=${ZERO} to launch WETH-paired only`);
    else ok(`V3 router ${v3Router}`);
  } else {
    ok("no V3 router: WETH-paired launches only");
  }
  if (weth) {
    // A wrong WETH is the quietest of these mistakes: the stack deploys, and
    // every pool it creates is against a token nobody holds.
    try {
      const w = await ethers.getContractAt(["function symbol() view returns (string)", "function decimals() view returns (uint8)"], weth);
      const [symbol, decimals] = [await w.symbol(), Number(await w.decimals())];
      if (decimals !== 18) fail(`WETH ${weth} reports ${decimals} decimals`);
      else ok(`WETH answers symbol ${symbol}, 18 decimals`);
    } catch {
      fail(`WETH ${weth} does not answer symbol()/decimals(); it is not an ERC-20`);
    }
  }

  console.log("\nimmutable choices");
  // These are the ones that cannot be changed after the deploy. Each is checked
  // for being set, for not being the deployer by accident, and for being able to
  // hold what it is about to be sent.
  const admin = required("ADMIN");
  const treasuries: Array<[string, string | undefined]> = [
    ["TENANT_TREASURY", process.env.TENANT_TREASURY ? required("TENANT_TREASURY") : undefined],
    ["PROTOCOL_TREASURY", process.env.PROTOCOL_TREASURY ? required("PROTOCOL_TREASURY") : undefined],
    ["TREASURY", process.env.TREASURY ? required("TREASURY") : undefined],
  ];
  const signer = signers[0];
  if (admin && signer && admin === signer.address) {
    // Not wrong, but the admin outlives the deploy by years and the deployer key
    // is normally a hot key that does not.
    warn("ADMIN is the deployer. SyndicateRegistry's admin is immutable, so this key can never be rotated out of it");
  } else if (admin) {
    ok(`ADMIN ${admin}`);
  }
  for (const [name, addr] of treasuries) {
    if (!addr) continue;
    const code = await ethers.provider.getCode(addr);
    if (code === "0x") {
      ok(`${name} ${addr} (EOA)`);
    } else {
      // A contract treasury that reverts on plain ETH strands every ETH-denominated
      // fee the splitter ever tries to forward to it.
      warn(`${name} ${addr} is a contract. It must accept a plain ETH transfer, or ETH fees to it will revert`);
    }
    if (signer && addr === signer.address) warn(`${name} is the deployer key`);
  }
  const operator = process.env.OPERATOR ? required("OPERATOR") : undefined;
  if (operator) {
    const bal = await ethers.provider.getBalance(operator);
    ok(`OPERATOR ${operator}, balance ${ethers.formatEther(bal)} ETH`);
    if (bal === 0n) warn("OPERATOR has no balance, so it cannot launch until it is funded");
  }

  console.log("\nnumbers");
  const bpsChecks: Array<[string, string | undefined, number, number]> = [
    ["PROTOCOL_BPS", process.env.PROTOCOL_BPS, 0, 10_000],
    ["PLATFORM_FEE_BPS", process.env.PLATFORM_FEE_BPS, 50, 100],
    ["REF_SHARE_BPS", process.env.REF_SHARE_BPS, 0, 5_000],
    ["FEE_BPS", process.env.FEE_BPS, 0, 100],
    ["REBATE_BPS", process.env.REBATE_BPS, 0, 10_000],
  ];
  for (const [name, raw, lo, hi] of bpsChecks) {
    if (raw === undefined) continue;
    const n = Number(raw);
    if (!Number.isInteger(n) || n < lo || n > hi) fail(`${name}=${raw} is outside ${lo}..${hi}`);
    else ok(`${name}=${n}`);
  }

  // The curve's start price is an FDV, not a number of wei, so the smallest
  // raise that can graduate depends on the ETH/USD price the launches pass. The
  // factory starts every coin at START_MCAP_USD_8 = $750 across a supply of
  // 1e9, which in ETH is $750 / price -- and k, the curve's slope, is computed
  // as 2*(target - p0*supply)/supply^2, so a target below that underflows and
  // every launch reverts with InvalidParams.
  //
  // This is the one coupling in the parameters that is invisible from either
  // side: 0.004 ETH is a perfectly reasonable graduation target and $3,000 is a
  // perfectly reasonable ETH price, and together they are a stack that cannot
  // launch anything. It was found by a launch reverting on a fork, which is the
  // cheap way to find it.
  const ethUsd = Number(process.env.ETH_USD ?? 0);
  if (ethUsd > 0) {
    console.log("\ncurve floor");
    const price8 = BigInt(Math.round(ethUsd)) * 100_000_000n;
    const floorWei = (750n * 100_000_000n * 10n ** 18n) / price8;
    ok(`at $${ethUsd.toLocaleString()}/ETH the curve starts at ${ethers.formatEther(floorWei)} ETH of FDV`);
    for (const [name, raw] of [["GRADUATION_RAISE_ETH", process.env.GRADUATION_RAISE_ETH], ["MIN_TARGET_ETH", process.env.MIN_TARGET_ETH]] as const) {
      if (!raw) continue;
      const wei = ethers.parseEther(raw);
      if (wei <= floorWei) {
        fail(`${name}=${raw} ETH is at or below that: every Open-mode launch at this price reverts. It must exceed ${ethers.formatEther(floorWei)} ETH`);
      } else {
        ok(`${name}=${raw} ETH clears it`);
      }
    }
    // The Launcher's first buy happens inside the factory's 60-second launch
    // window, where the per-wallet cap is target/100. Over that and the launch
    // reverts, mined salt and all.
    if (process.env.GRADUATION_RAISE_ETH) {
      const cap = ethers.parseEther(process.env.GRADUATION_RAISE_ETH) / 100n;
      ok(`first buy in the launch window is capped at ${ethers.formatEther(cap)} ETH (target/100)`);
    }
  } else {
    warn("ETH_USD is not set, so the curve's start FDV was not checked against the graduation target");
  }

  console.log("");
  if (local) console.log("This RPC is a local development node. Nothing here was checked against a real chain.");
  if (failures) {
    console.log(`${failures} failure(s), ${warnings} warning(s). Do not deploy.`);
    process.exit(1);
  }
  console.log(`0 failures, ${warnings} warning(s). ${warnings ? "Read the warnings, then deploy." : "Clear to deploy."}`);
}
main().catch((e) => { console.error(e); process.exit(1); });
