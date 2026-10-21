import { expect } from "chai";
import { ethers } from "hardhat";

// The moat. The tenant model's documented lock-in — that leaving costs a brand
// our keepers, index and routing — did not exist in the contracts: a tenant
// gets its own whole stack and the fee split is only a matter of where fees
// happen to be addressed.
//
// Hilferding's cartel binds "not only by the terms of the agreement": members
// sell through "a central sales agency", so a departing one "has to renew
// relations with its customers, re-establish its sales outlets". The router is
// that agency, and these tests pin that membership pays and leaving stops it.

const FEE_BPS = 100n; // 1% of the trade
/** Far enough ahead that no test is racing the clock. */
const LATER = 2n ** 48n;
const REBATE_BPS = 3000n; // 30% of that fee to the coin's launchpad
const BPS = 10_000n;

function encodePath(tokenIn: string, fee: number, tokenOut: string): string {
  return ethers.concat([tokenIn, ethers.toBeHex(fee, 3), tokenOut]);
}

/** Stands in for a venture factory: the registry asks it to confirm a coin. */
async function deployFakeFactory(known: string[]) {
  const f = await (await ethers.getContractFactory("FakeListings")).deploy();
  for (const coin of known) await (await f.setCreator(coin, "0x000000000000000000000000000000000000dEaD")).wait();
  return f;
}

async function setup(rebateBps: bigint = REBATE_BPS) {
  const [admin, trader, treasury, tenant, outsider] = await ethers.getSigners();
  const weth = await (await ethers.getContractFactory("WETH9")).deploy();
  const coin = await (await ethers.getContractFactory("MockERC20")).deploy();
  const v3 = await (await ethers.getContractFactory("MockV3Router")).deploy();
  const registry = await (await ethers.getContractFactory("SyndicateRegistry")).deploy(admin.address);
  const router = await (await ethers.getContractFactory("AggregatorRouter")).deploy(
    await weth.getAddress(),
    await v3.getAddress(),
    await weth.getAddress(),
    treasury.address,
    FEE_BPS,
    await registry.getAddress(),
    rebateBps,
  );
  const factory = await deployFakeFactory([await coin.getAddress()]);
  return { admin, trader, treasury, tenant, outsider, weth, coin, v3, registry, router, factory };
}

/** A trade of `amountIn` WETH into the coin. Returns the fee charged. */
async function trade(s: Awaited<ReturnType<typeof setup>>, amountIn: bigint) {
  const { trader, weth, coin, v3, router } = s;
  await (await weth.connect(trader).deposit({ value: amountIn })).wait();
  await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
  await (await coin.mint(await v3.getAddress(), amountIn * 2n)).wait();
  const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
  await (await router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, LATER)).wait();
  return (amountIn * FEE_BPS) / BPS;
}

describe("SyndicateRegistry", function () {
  this.timeout(300_000);

  it("refuses a zero admin", async () => {
    const F = await ethers.getContractFactory("SyndicateRegistry");
    await expect(F.deploy(ethers.ZeroAddress)).to.be.revertedWithCustomError(F, "NotAdmin");
  });

  it("only the admin admits or removes a launchpad", async () => {
    const { registry, outsider, tenant, factory } = await setup();
    await expect(registry.connect(outsider).setFactory(await factory.getAddress(), tenant.address))
      .to.be.revertedWithCustomError(registry, "NotAdmin");
  });

  it("will not register a coin for a launchpad that is not a member", async () => {
    const { registry, coin, factory } = await setup();
    await expect(registry.registerCoin(await factory.getAddress(), await coin.getAddress()))
      .to.be.revertedWithCustomError(registry, "NotAMember");
  });

  it("asks the factory to confirm the coin, so nobody can claim another's", async () => {
    const { admin, registry, factory, tenant, weth } = await setup();
    await (await registry.setFactory(await factory.getAddress(), tenant.address)).wait();
    // A coin that factory never launched: the registry checks and refuses.
    await expect(registry.registerCoin(await factory.getAddress(), await weth.getAddress()))
      .to.be.revertedWithCustomError(registry, "NotFromThatFactory");
    void admin;
  });

  it("registers permissionlessly once the factory confirms it", async () => {
    const { registry, factory, coin, tenant, outsider } = await setup();
    await (await registry.setFactory(await factory.getAddress(), tenant.address)).wait();
    // Anyone may call it; the verification is what matters, not the caller.
    await expect(registry.connect(outsider).registerCoin(await factory.getAddress(), await coin.getAddress()))
      .to.emit(registry, "CoinRegistered");
    expect(await registry.beneficiaryFor(await coin.getAddress())).to.equal(tenant.address);
    await expect(registry.registerCoin(await factory.getAddress(), await coin.getAddress()))
      .to.be.revertedWithCustomError(registry, "AlreadyRegistered");
  });

  it("reads the beneficiary live, so removal reaches every coin at once", async () => {
    const { registry, factory, coin, tenant, outsider } = await setup();
    const f = await factory.getAddress();
    await (await registry.setFactory(f, tenant.address)).wait();
    await (await registry.registerCoin(f, await coin.getAddress())).wait();
    expect(await registry.beneficiaryFor(await coin.getAddress())).to.equal(tenant.address);

    // Point the launchpad at a different payee.
    await (await registry.setFactory(f, outsider.address)).wait();
    expect(await registry.beneficiaryFor(await coin.getAddress())).to.equal(outsider.address);

    // Remove it: a snapshot taken at registration would still be paying.
    await (await registry.setFactory(f, ethers.ZeroAddress)).wait();
    expect(await registry.beneficiaryFor(await coin.getAddress())).to.equal(ethers.ZeroAddress);
  });

  it("reports nobody for a coin it has never seen", async () => {
    const { registry, weth } = await setup();
    expect(await registry.beneficiaryFor(await weth.getAddress())).to.equal(ethers.ZeroAddress);
  });
});

describe("AggregatorRouter: the syndicate rebate", function () {
  this.timeout(300_000);

  it("pays a member a share of the fee on its coin's volume", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();

    const amountIn = ethers.parseEther("10");
    const fee = await trade(s, amountIn);
    const rebate = (fee * REBATE_BPS) / BPS;

    expect(await s.router.owed(s.tenant.address, await s.weth.getAddress())).to.equal(rebate);
    // The treasury keeps the rest, and the two together are the whole fee.
    expect(await s.weth.balanceOf(s.treasury.address)).to.equal(fee - rebate);
    expect(await s.weth.balanceOf(await s.router.getAddress())).to.equal(rebate);
  });

  it("gives the treasury the whole fee for a coin nobody is a member for", async () => {
    const s = await setup();
    const fee = await trade(s, ethers.parseEther("5"));
    expect(await s.weth.balanceOf(s.treasury.address)).to.equal(fee);
    expect(await s.router.owed(s.tenant.address, await s.weth.getAddress())).to.equal(0n);
  });

  it("leaving stops the stream: this is the exit cost", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    const wethAddr = await s.weth.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();

    const fee1 = await trade(s, ethers.parseEther("4"));
    const earned = (fee1 * REBATE_BPS) / BPS;
    expect(await s.router.owed(s.tenant.address, wethAddr)).to.equal(earned);

    // The launchpad leaves the syndicate.
    await (await s.registry.setFactory(f, ethers.ZeroAddress)).wait();

    // The same trade on the same coin now earns them nothing, and the whole
    // fee goes to the treasury. Their coins keep trading; they stop being paid
    // for it. Nothing was clawed back — only the future was lost.
    const before = await s.weth.balanceOf(s.treasury.address);
    const fee2 = await trade(s, ethers.parseEther("4"));
    expect(await s.router.owed(s.tenant.address, wethAddr)).to.equal(earned);
    expect((await s.weth.balanceOf(s.treasury.address)) - before).to.equal(fee2);

    // What they had already earned stays claimable: an exit cost, not a seizure.
    await (await s.router.connect(s.tenant).claimRebate(wethAddr)).wait();
    expect(await s.weth.balanceOf(s.tenant.address)).to.equal(earned);
  });

  it("pays on a sell of the member's coin as well as a buy", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();

    // Coin in, WETH out: the input side is the member's coin.
    const amountIn = ethers.parseEther("3");
    await (await s.coin.mint(s.trader.address, amountIn)).wait();
    await (await s.coin.connect(s.trader).approve(await s.router.getAddress(), amountIn)).wait();
    await (await s.weth.deposit({ value: ethers.parseEther("50") })).wait();
    await (await s.weth.transfer(await s.v3.getAddress(), ethers.parseEther("50"))).wait();
    const path = encodePath(await s.coin.getAddress(), 3000, await s.weth.getAddress());
    await (await s.router.connect(s.trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, LATER)).wait();

    const fee = (amountIn * FEE_BPS) / BPS;
    // The rebate is paid in whatever the trader spent, which here is the coin.
    expect(await s.router.owed(s.tenant.address, await s.coin.getAddress())).to.equal((fee * REBATE_BPS) / BPS);
  });

  it("cannot be diverted: the coin comes from the pool, not from the caller", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();

    // An unrelated pair. There is no argument by which a trader could name the
    // member's coin and collect its rebate on this trade.
    const other = await (await ethers.getContractFactory("MockERC20")).deploy();
    const amountIn = ethers.parseEther("2");
    await (await s.weth.connect(s.trader).deposit({ value: amountIn })).wait();
    await (await s.weth.connect(s.trader).approve(await s.router.getAddress(), amountIn)).wait();
    await (await other.mint(await s.v3.getAddress(), amountIn * 2n)).wait();
    const path = encodePath(await s.weth.getAddress(), 3000, await other.getAddress());
    await (await s.router.connect(s.trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, LATER)).wait();

    expect(await s.router.owed(s.tenant.address, await s.weth.getAddress())).to.equal(0n);
    expect(await s.weth.balanceOf(s.treasury.address)).to.equal((amountIn * FEE_BPS) / BPS);
  });

  it("accrues rather than pushes, so a broken member cannot fail a trade", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    // The beneficiary is a contract that rejects everything sent to it.
    const rejecter = await (await ethers.getContractFactory("EthRejecter")).deploy();
    await (await s.registry.setFactory(f, await rejecter.getAddress())).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();
    // A stranger's trade still goes through.
    const fee = await trade(s, ethers.parseEther("1"));
    expect(await s.router.owed(await rejecter.getAddress(), await s.weth.getAddress()))
      .to.equal((fee * REBATE_BPS) / BPS);
  });

  it("refuses a claim with nothing owed, and pays only once", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    const wethAddr = await s.weth.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();
    await expect(s.router.connect(s.tenant).claimRebate(wethAddr))
      .to.be.revertedWithCustomError(s.router, "NothingOwed");

    await trade(s, ethers.parseEther("2"));
    await (await s.router.connect(s.tenant).claimRebate(wethAddr)).wait();
    await expect(s.router.connect(s.tenant).claimRebate(wethAddr))
      .to.be.revertedWithCustomError(s.router, "NothingOwed");
  });

  it("keeps the whole fee when the rebate is switched off", async () => {
    const s = await setup(0n);
    const f = await s.factory.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();
    const fee = await trade(s, ethers.parseEther("2"));
    expect(await s.weth.balanceOf(s.treasury.address)).to.equal(fee);
    expect(await s.router.owed(s.tenant.address, await s.weth.getAddress())).to.equal(0n);
  });

  it("never pays out more than it took, over many random trades", async () => {
    const s = await setup();
    const f = await s.factory.getAddress();
    const wethAddr = await s.weth.getAddress();
    await (await s.registry.setFactory(f, s.tenant.address)).wait();
    await (await s.registry.registerCoin(f, await s.coin.getAddress())).wait();

    let seed = 0x2addn;
    let charged = 0n;
    for (let i = 0; i < 25; i++) {
      seed = (seed * 6364136223846793005n + 1442695040888963407n) & ((1n << 64n) - 1n);
      const amountIn = (seed % 10n ** 17n) + 10n ** 12n;
      charged += await trade(s, amountIn);
    }
    const owed = await s.router.owed(s.tenant.address, wethAddr);
    const treasury = await s.weth.balanceOf(s.treasury.address);
    expect(owed + treasury).to.equal(charged);
    // Rounding favours the treasury, never the payout.
    expect(owed).to.be.lessThanOrEqual((charged * REBATE_BPS) / BPS);
  });
});
