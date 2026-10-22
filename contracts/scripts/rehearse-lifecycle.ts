import { ethers } from "hardhat";
import { readFileSync } from "fs";

import { isLocalNode } from "./lib/venture-stack";

// The whole money path on one deployed stack: launch, buy to graduation, trade
// the graduated pool, pull the fees into the splitter, split them.
//
//   RPC_URL=http://127.0.0.1:8545 CHAIN_ID=… PRIVATE_KEY=<the operator's> \
//   TENANT_DEPLOYMENT=… LAUNCHER_DEPLOYMENT=… \
//   npx hardhat run scripts/rehearse-lifecycle.ts --network target
//
// It exists to answer one question before any real ETH is spent: what does it
// cost to get a single coin all the way to earning, and does the stack as
// deployed actually carry it there? Every number it prints is a receipt or a
// balance, not an estimate.
//
// The raise is the expensive part and it is not recoverable: graduated
// liquidity is owned by the factory and can only be added to, never removed
// (VentureFactory._graduate). So the graduation target is the price of the
// experiment, and the point of running this is to find the smallest one the
// stack will carry.
//
// Needs evm_increaseTime for the 60-second launch window, so: a fork.
async function main() {
  const dep = JSON.parse(readFileSync(process.env.LAUNCHER_DEPLOYMENT!, "utf8"));
  const tenant = JSON.parse(readFileSync(process.env.TENANT_DEPLOYMENT!, "utf8"));
  const c = dep.contracts;

  const factory = await ethers.getContractAt("VentureFactory", c.factory);
  const target = await factory.graduationRaiseWei();
  const ethUsd8 = BigInt(process.env.ETH_USD_8 ?? tenant.ethUsdBand8[1]);
  console.log(`graduation target ${ethers.formatEther(target)} ETH, ETH/USD $${(Number(ethUsd8) / 1e8).toLocaleString()}`);
  const curveFloor = (750n * 100_000_000n * 10n ** 18n) / ethUsd8;
  console.log(`curve start FDV ${ethers.formatEther(curveFloor)} ETH  (target must exceed it)`);

  const p = {
    name: `Life${process.env.TAG ?? Date.now() % 1_000_000}`,
    symbol: `LIF${process.env.TAG ?? Date.now() % 1_000_000}`,
    metadataURI: "", pair: c.weth, buyTaxBps: 300, sellTaxBps: 300,
    devWallet: c.creatorSplitter, devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500,
    ethUsdPrice8: ethUsd8, targetRaiseWei: target, raiseDurationSecs: 0,
    maxBuyWei: target, founderRaiseBps: 0, founderSupplyBps: 0, vestingSecs: 0,
    mode: 1, minHoldForDividends: 0, dividendMode: 0, v3Path: "0x",
  };

  // Salt first: the grind is pure keccak and takes long enough that an idle RPC
  // connection would be closed under it.
  const Token = await ethers.getContractFactory("QuiverToken");
  const hash = ethers.keccak256(ethers.concat([Token.bytecode, ethers.AbiCoder.defaultAbiCoder().encode(
    ["string", "string", "string", "uint256", "address", "address", "uint16", "address", "uint256", "uint8"],
    [p.name, p.symbol, p.metadataURI, 10n ** 27n, c.launcher, c.factory, p.buyTaxBps, p.pair, 0n, p.dividendMode],
  )]));
  const mark = BigInt(tenant.vanity);
  let salt = "";
  for (let i = 0n; i < 8_000_000n; i++) {
    const s = ethers.zeroPadValue(ethers.toBeHex(i), 32);
    if ((BigInt(ethers.getCreate2Address(c.tokenDeployer, s, hash)) >> 144n) === mark) { salt = s; break; }
  }
  if (!salt) throw new Error("no vanity salt");

  const [operator] = await ethers.getSigners();
  if (operator.address.toLowerCase() !== String(dep.operator).toLowerCase()) {
    throw new Error(`PRIVATE_KEY is ${operator.address}; this Launcher only accepts ${dep.operator}`);
  }
  const launcher = await ethers.getContractAt("Launcher", c.launcher);

  let gas = 0n;
  let principal = 0n;
  // Price the gas at the real chain's rate, not the fork's. A hardhat node
  // invents its own base fee, which on this chain is about a hundred times the
  // real one, so a cost measured at the fork's price is a cost measured for
  // nowhere. Testnet 46630 reports 0.01 gwei, mainnet 4663 about 0.0213.
  const price = BigInt(process.env.GAS_PRICE_WEI ?? 10_000_000n);

  // 1) Launch, with the first buy at the launch window's per-wallet cap.
  const firstBuy = target / 100n;
  const r1 = await (await launcher.launch(c.factory, p, salt, ethers.id("rehearsal"), 0, { value: firstBuy })).wait();
  gas += r1!.gasUsed; principal += firstBuy;
  const coin = await factory.allTokens((await factory.totalTokens()) - 1n);
  console.log(`\nlaunched ${coin}  first buy ${ethers.formatEther(firstBuy)} ETH  ${r1!.gasUsed} gas`);

  // Past the factory's 60-second launch window, whose tighter per-wallet cap
  // would otherwise bind every buy below. A development node can be told to
  // skip ahead; a real chain has to be waited out.
  if (await isLocalNode()) {
    await ethers.provider.send("evm_increaseTime", [61]);
    await ethers.provider.send("evm_mine", []);
  } else {
    console.log("waiting out the 60s launch window...");
    await new Promise((done) => setTimeout(done, 65_000));
  }

  // 2) Buy to graduation. The curve takes what it needs and refunds the rest,
  //    so overshooting the last buy is free apart from gas.
  let buys = 0;
  for (let i = 0; i < 40; i++) {
    const st = await factory.curveState(coin);
    if (st.finalized) break;
    const left = target - st.raisedWei;
    const step = left > target / 4n ? target / 4n : left + target / 1000n;
    const r = await (await factory.buy(coin, 0, { value: step })).wait();
    gas += r!.gasUsed; buys++;
    const after = await factory.curveState(coin);
    principal += after.raisedWei - st.raisedWei;
  }
  const st = await factory.curveState(coin);
  if (!st.finalized) throw new Error(`not graduated after ${buys} buys: raised ${ethers.formatEther(st.raisedWei)} of ${ethers.formatEther(target)}`);
  console.log(`graduated after ${buys} buys, raised ${ethers.formatEther(st.raisedWei)} ETH`);

  // 3) Trade the graduated pool. This is the only step that produces the
  //    recurring income the whole business rests on: the hook's fee on a pool
  //    whose liquidity can never leave.
  const router = await ethers.getContractAt("VentureRouter", c.router);
  const swap = BigInt(process.env.SWAP_WEI ?? (target / 20n).toString());
  const splitter = await ethers.getContractAt("FeeSplitter", tenant.tenant.splitter);
  const before = await ethers.provider.getBalance(tenant.tenant.splitter);
  let swaps = 0;
  for (let i = 0; i < Number(process.env.SWAPS ?? 3); i++) {
    try {
      // SWAP_GAS_LIMIT unset lets the provider estimate, which is what any
      // ordinary caller does. The first trade on a freshly graduated pool has
      // been seen to under-estimate and die with "ran out of gas"; set it to
      // pin that down or to work around it.
      const est = await router.buy.estimateGas(coin, "0x", 0, { value: swap }).catch(() => -1n);
      console.log(`  swap ${i + 1}: estimateGas ${est}`);
      const limit = process.env.SWAP_GAS_LIMIT ? { gasLimit: Number(process.env.SWAP_GAS_LIMIT) } : {};
      const r = await (await router.buy(coin, "0x", 0, { value: swap, ...limit })).wait();
      gas += r!.gasUsed; principal += swap; swaps++;
    } catch (e: any) {
      console.log(`  swap ${i + 1} reverted: ${e?.shortMessage ?? e?.message}`);
      break;
    }
  }
  console.log(`${swaps} swap(s) of ${ethers.formatEther(swap)} ETH each`);

  // 4) What arrived, and where it ends up.
  const accrued = await factory.feesAccrued(tenant.tenant.splitter);
  const held = await ethers.provider.getBalance(tenant.tenant.splitter);
  const coinHeld = await (await ethers.getContractAt("QuiverToken", coin)).balanceOf(tenant.tenant.splitter);
  console.log(`\nplatform splitter: ${ethers.formatEther(held - before)} ETH held, ${ethers.formatEther(accrued)} ETH in the factory ledger, ${ethers.formatEther(coinHeld)} of the coin`);
  const creatorAccrued = await factory.feesAccrued(c.launcher);
  console.log(`launcher (creator income): ${ethers.formatEther(creatorAccrued)} ETH in the factory ledger`);

  if (accrued > 0n) {
    const r = await (await splitter.pullFactory(c.factory)).wait();
    gas += r!.gasUsed;
  }
  const splittable = await splitter.splittable(ethers.ZeroAddress);
  if (splittable > 0n) {
    const t0 = await ethers.provider.getBalance(tenant.tenant.treasury);
    const p0 = await ethers.provider.getBalance(tenant.tenant.protocolTreasury);
    const r = await (await splitter.split(ethers.ZeroAddress)).wait();
    gas += r!.gasUsed;
    const toTenant = (await ethers.provider.getBalance(tenant.tenant.treasury)) - t0;
    const toProtocol = (await ethers.provider.getBalance(tenant.tenant.protocolTreasury)) - p0;
    console.log(`split ${ethers.formatEther(splittable)} ETH: tenant ${ethers.formatEther(toTenant)}, protocol ${ethers.formatEther(toProtocol)} (${tenant.tenant.protocolBps} bps)`);
  } else {
    console.log("nothing in ETH to split yet: the fees are in the coin, which is what the keeper converts");
  }

  console.log(`\ncost of one coin, launch to earning:`);
  console.log(`  gas       ${gas} gas = ${ethers.formatEther(gas * price)} ETH`);
  console.log(`  principal ${ethers.formatEther(principal)} ETH  (the raise is locked in the pool for good; the swaps bought the coin)`);
  console.log(`  total     ${ethers.formatEther(gas * price + principal)} ETH`);
}
main().catch((e) => { console.error(e); process.exit(1); });
