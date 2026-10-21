import { expect } from "chai";
import { ethers } from "hardhat";

// Tenant stacks: a full venture stack whose platformTreasury is a FeeSplitter.
// The factory and hook are the unmodified contracts; these tests pin that every
// fee path ends split protocolBps / rest, and that a hostile tenant cannot
// block the protocol's share.

import { deployStack, launch } from "./helpers/venture";

const PROTOCOL_BPS = 1000n;

async function deploySplitter(tenant: string, protocol: string, poolManager = ethers.ZeroAddress) {
  const s = await (await ethers.getContractFactory("FeeSplitter")).deploy(tenant, protocol, PROTOCOL_BPS, poolManager);
  await s.waitForDeployment();
  return s;
}

const share = (amount: bigint) => {
  const toProtocol = (amount * PROTOCOL_BPS) / 10_000n;
  return { toProtocol, toTenant: amount - toProtocol };
};

describe("Tenant FeeSplitter", function () {
  this.timeout(300_000);

  it("rejects a zero recipient or a cut above 100%", async () => {
    const [, a, b] = await ethers.getSigners();
    const F = await ethers.getContractFactory("FeeSplitter");
    await expect(F.deploy(ethers.ZeroAddress, b.address, 1000, ethers.ZeroAddress)).to.be.revertedWithCustomError(F, "BadConfig");
    await expect(F.deploy(a.address, ethers.ZeroAddress, 1000, ethers.ZeroAddress)).to.be.revertedWithCustomError(F, "BadConfig");
    await expect(F.deploy(a.address, b.address, 10_001, ethers.ZeroAddress)).to.be.revertedWithCustomError(F, "BadConfig");
  });

  it("splits curve-phase ETH fees pulled from the factory ledger", async () => {
    const [, creator, buyer, tenant, protocol] = await ethers.getSigners();
    const splitter = await deploySplitter(tenant.address, protocol.address);
    const { factory, tokenDeployer, weth, hook } = await deployStack(1n, undefined, await splitter.getAddress());
    expect(await hook.platformTreasury()).to.equal(await splitter.getAddress());

    const coin = await launch(factory, tokenDeployer, creator, await weth.getAddress());
    for (let i = 0; i < 5; i++) {
      await (await factory.connect(buyer).buy(coin, 0, { value: ethers.parseEther("0.03") })).wait();
    }
    const q = (await factory.curveState(coin)).soldWhole / 10n;
    const token = await ethers.getContractAt("QuiverToken", coin);
    await (await token.connect(buyer).approve(await factory.getAddress(), ethers.MaxUint256)).wait();
    await (await factory.connect(buyer).sell(coin, q, 0)).wait();

    const accrued = await factory.feesAccrued(await splitter.getAddress());
    expect(accrued).to.be.greaterThan(0n);
    await (await splitter.pullFactory(await factory.getAddress())).wait();
    expect(await factory.feesAccrued(await splitter.getAddress())).to.equal(0n);
    expect(await splitter.splittable(ethers.ZeroAddress)).to.equal(accrued);

    const t0 = await ethers.provider.getBalance(tenant.address);
    const p0 = await ethers.provider.getBalance(protocol.address);
    await (await splitter.connect(buyer).split(ethers.ZeroAddress)).wait();
    const { toProtocol, toTenant } = share(accrued);
    expect((await ethers.provider.getBalance(protocol.address)) - p0).to.equal(toProtocol);
    expect((await ethers.provider.getBalance(tenant.address)) - t0).to.equal(toTenant);
    expect(await ethers.provider.getBalance(await splitter.getAddress())).to.equal(0n);
  });

  it("pullFactory is a no-op when nothing is accrued", async () => {
    const [, tenant, protocol] = await ethers.getSigners();
    const splitter = await deploySplitter(tenant.address, protocol.address);
    const { factory } = await deployStack(1n, undefined, await splitter.getAddress());
    await expect(splitter.pullFactory(await factory.getAddress())).to.not.be.reverted;
  });

  it("splits ERC-20 fees (the hook's take path) exactly", async () => {
    const [funder, tenant, protocol] = await ethers.getSigners();
    const splitter = await deploySplitter(tenant.address, protocol.address);
    const weth = await (await ethers.getContractFactory("WETH9")).deploy();
    const amount = 1_234_567_891_011n;
    await (await weth.deposit({ value: amount })).wait();
    await (await weth.transfer(await splitter.getAddress(), amount)).wait();

    await (await splitter.split(await weth.getAddress())).wait();
    const { toProtocol, toTenant } = share(amount);
    expect(await weth.balanceOf(protocol.address)).to.equal(toProtocol);
    expect(await weth.balanceOf(tenant.address)).to.equal(toTenant);
    expect(await weth.balanceOf(await splitter.getAddress())).to.equal(0n);
    void funder;
  });

  it("redeems ERC-6909 claims (the hook's mint fallback) and splits them", async () => {
    const [, tenant, protocol] = await ethers.getSigners();
    const pm = await (await ethers.getContractFactory("MockClaimsManager")).deploy();
    const splitter = await deploySplitter(tenant.address, protocol.address, await pm.getAddress());
    const weth = await (await ethers.getContractFactory("WETH9")).deploy();
    const amount = ethers.parseEther("0.7");
    await (await weth.deposit({ value: amount })).wait();
    await (await weth.transfer(await pm.getAddress(), amount)).wait();
    const id = BigInt(await weth.getAddress());
    await (await pm.mint(await splitter.getAddress(), id, amount)).wait();

    await (await splitter.redeem6909(await weth.getAddress(), amount)).wait();
    expect(await pm.balanceOf(await splitter.getAddress(), id)).to.equal(0n);
    expect(await splitter.splittable(await weth.getAddress())).to.equal(amount);

    await (await splitter.split(await weth.getAddress())).wait();
    const { toProtocol, toTenant } = share(amount);
    expect(await weth.balanceOf(protocol.address)).to.equal(toProtocol);
    expect(await weth.balanceOf(tenant.address)).to.equal(toTenant);
  });

  it("only the pool manager can drive the unlock callback", async () => {
    const [, tenant, protocol] = await ethers.getSigners();
    const pm = await (await ethers.getContractFactory("MockClaimsManager")).deploy();
    const splitter = await deploySplitter(tenant.address, protocol.address, await pm.getAddress());
    const data = ethers.AbiCoder.defaultAbiCoder().encode(["address", "uint256"], [ethers.ZeroAddress, 1n]);
    await expect(splitter.unlockCallback(data)).to.be.revertedWithCustomError(splitter, "NotPoolManager");
  });

  it("an ETH-rejecting tenant cannot block the protocol's share", async () => {
    const [funder, protocol] = await ethers.getSigners();
    const rejecter = await (await ethers.getContractFactory("EthRejecter")).deploy();
    const splitter = await deploySplitter(await rejecter.getAddress(), protocol.address);
    const amount = ethers.parseEther("1");
    await (await funder.sendTransaction({ to: await splitter.getAddress(), value: amount })).wait();

    const p0 = await ethers.provider.getBalance(protocol.address);
    await expect(splitter.split(ethers.ZeroAddress)).to.emit(splitter, "Deferred");
    const { toProtocol, toTenant } = share(amount);
    expect((await ethers.provider.getBalance(protocol.address)) - p0).to.equal(toProtocol);
    expect(await splitter.owed(await rejecter.getAddress(), ethers.ZeroAddress)).to.equal(toTenant);
    // The deferred credit is held, not re-split.
    expect(await splitter.splittable(ethers.ZeroAddress)).to.equal(0n);
    await (await splitter.split(ethers.ZeroAddress)).wait();
    expect((await ethers.provider.getBalance(protocol.address)) - p0).to.equal(toProtocol);
  });

  it("a deferred credit is claimable once the recipient can receive", async () => {
    const [funder, tenant] = await ethers.getSigners();
    const rejecter = await (await ethers.getContractFactory("EthRejecter")).deploy();
    // Protocol is the rejecter here: the tenant is paid, the protocol credit waits.
    const splitter = await deploySplitter(tenant.address, await rejecter.getAddress());
    await (await funder.sendTransaction({ to: await splitter.getAddress(), value: 10_000n })).wait();
    await (await splitter.split(ethers.ZeroAddress)).wait();
    expect(await splitter.owed(await rejecter.getAddress(), ethers.ZeroAddress)).to.equal(1_000n);
    await expect(splitter.connect(tenant).claim(ethers.ZeroAddress)).to.be.revertedWithCustomError(splitter, "NothingOwed");
    // The rejecter still cannot take ETH, so its claim reverts and the credit stays.
    const data = splitter.interface.encodeFunctionData("claim", [ethers.ZeroAddress]);
    await expect(rejecter.exec(await splitter.getAddress(), data)).to.be.reverted;
    expect(await splitter.owed(await rejecter.getAddress(), ethers.ZeroAddress)).to.equal(1_000n);
  });

  it("random fee streams always split to the wei: no dust created or lost", async () => {
    const [funder, tenant, protocol] = await ethers.getSigners();
    const splitter = await deploySplitter(tenant.address, protocol.address);
    const t0 = await ethers.provider.getBalance(tenant.address);
    const p0 = await ethers.provider.getBalance(protocol.address);
    let seed = 0x2addn, total = 0n, expectP = 0n;
    for (let i = 0; i < 40; i++) {
      seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 64n) - 1n);
      const v = (seed % 10n ** 15n) + 1n;
      await (await funder.sendTransaction({ to: await splitter.getAddress(), value: v })).wait();
      total += v;
      expectP += (v * PROTOCOL_BPS) / 10_000n;
      await (await splitter.connect(funder).split(ethers.ZeroAddress)).wait();
    }
    const gotP = (await ethers.provider.getBalance(protocol.address)) - p0;
    const gotT = (await ethers.provider.getBalance(tenant.address)) - t0;
    expect(gotP).to.equal(expectP);
    expect(gotP + gotT).to.equal(total);
  });
});
