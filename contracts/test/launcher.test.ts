import { expect } from "chai";
import { ethers, network } from "hardhat";

// The Launcher against the vendored venture stack. Mirrors DoublePlus's test
// fixture (test/v4/helpers/venture.ts): the curve phase runs on the plain
// hardhat network with placeholder pool-manager/V3 addresses, the vanity mark
// is off, and the hook is real so launch() can read its guardrails.

const ETH_USD_8 = 1865n * 10n ** 8n;
const BAND: [bigint, bigint] = [1_000n * 10n ** 8n, 10_000n * 10n ** 8n];
const PROTOCOL_BPS = 1000n;

async function deployVenture(platformTreasury: string) {
  const [admin] = await ethers.getSigners();
  const weth = await (await ethers.getContractFactory("WETH9")).deploy();
  const placeholder = await weth.getAddress();
  const vestingDeployer = await (await ethers.getContractFactory("VestingDeployer")).deploy();
  const nonce = await ethers.provider.getTransactionCount(admin.address);
  const predicted = ethers.getCreateAddress({ from: admin.address, nonce: nonce + 2 });
  const hook = await (await ethers.getContractFactory("VentureFeeHook")).deploy(placeholder, platformTreasury, predicted, 100, 2000);
  const tokenDeployer = await (await ethers.getContractFactory("VentureTokenDeployer")).deploy(predicted);
  const factory = await (await ethers.getContractFactory("VentureFactory")).deploy(
    admin.address, admin.address, placeholder, await hook.getAddress(), placeholder, placeholder,
    await vestingDeployer.getAddress(), await tokenDeployer.getAddress(), 50, 100, 1n, 0, BAND[0], BAND[1],
  );
  expect(await factory.getAddress()).to.equal(predicted);
  // Open-mode graduation trigger and a creator curve share of 10%.
  await (await factory.setParams(0, ethers.parseEther("4"), 365 * 86_400, 1000)).wait();
  return { factory, hook, weth };
}

async function deploySplitter(tenant: string, protocol: string) {
  return (await ethers.getContractFactory("FeeSplitter")).deploy(tenant, protocol, PROTOCOL_BPS, ethers.ZeroAddress);
}

function params(weth: string, devWallet: string, overrides: Record<string, unknown> = {}) {
  return {
    name: "Agent", symbol: "AGT", metadataURI: "", pair: weth,
    buyTaxBps: 300, sellTaxBps: 300, devWallet,
    devBps: 2500, dividendBps: 2500, liquidityBps: 2500, mmBps: 2500,
    ethUsdPrice8: ETH_USD_8, targetRaiseWei: 0n, raiseDurationSecs: 0, maxBuyWei: 0n,
    founderRaiseBps: 0, founderSupplyBps: 0, vestingSecs: 0,
    mode: 1, minHoldForDividends: 0n, dividendMode: 0, v3Path: "0x",
    ...overrides,
  };
}

async function setup() {
  const [admin, operator, tenant, protocol, trader, stranger] = await ethers.getSigners();
  const platform = await deploySplitter(tenant.address, protocol.address);
  const creator = await deploySplitter(tenant.address, protocol.address);
  const { factory, weth } = await deployVenture(await platform.getAddress());
  const launcher = await (await ethers.getContractFactory("Launcher")).deploy(operator.address, await creator.getAddress());
  return { admin, operator, tenant, protocol, trader, stranger, platform, creator, factory, weth, launcher };
}

const SPEC = ethers.id("spec:test");

describe("Launcher", function () {
  this.timeout(300_000);

  it("rejects a zero operator or splitter", async () => {
    const [a] = await ethers.getSigners();
    const L = await ethers.getContractFactory("Launcher");
    await expect(L.deploy(ethers.ZeroAddress, a.address)).to.be.revertedWithCustomError(L, "BadConfig");
    await expect(L.deploy(a.address, ethers.ZeroAddress)).to.be.revertedWithCustomError(L, "BadConfig");
  });

  it("launches and first-buys in one transaction, as the coin's creator", async () => {
    const { operator, factory, weth, launcher, creator } = await setup();
    const p = params(await weth.getAddress(), await creator.getAddress());
    const firstBuy = ethers.parseEther("0.03");
    const tx = launcher.connect(operator).launch(await factory.getAddress(), p, ethers.ZeroHash, SPEC, 0, { value: firstBuy });
    await expect(tx).to.emit(launcher, "Launched");
    const coin = await factory.allTokens(0);
    expect((await factory.listings(coin)).creator).to.equal(await launcher.getAddress());

    const token = await ethers.getContractAt("QuiverToken", coin);
    const held = await token.balanceOf(operator.address);
    expect(held).to.be.greaterThan(0n);
    expect(await token.balanceOf(await launcher.getAddress())).to.equal(0n);
    expect(await ethers.provider.getBalance(await launcher.getAddress())).to.equal(0n);

    const rc = await (await tx).wait();
    const ev = rc!.logs.map((l) => { try { return launcher.interface.parseLog(l); } catch { return null; } }).find((e) => e?.name === "Launched");
    expect(ev!.args.specId).to.equal(SPEC);
    expect(ev!.args.token).to.equal(coin);
    expect(ev!.args.tokensOut).to.equal(held);
  });

  it("a first buy above the launch-window cap reverts the launch; at the cap it lands", async () => {
    const { operator, factory, weth, launcher, creator } = await setup();
    // Open mode targets graduationRaiseWei (4 ETH); for 60 s each wallet may
    // put at most 1% of it (0.04 ETH net of the entry fee) into the curve.
    const p = params(await weth.getAddress(), await creator.getAddress());
    const f = await factory.getAddress();
    await expect(launcher.connect(operator).launch(f, p, ethers.ZeroHash, SPEC, 0, { value: ethers.parseEther("0.05") }))
      .to.be.revertedWithCustomError(factory, "CapExceeded");
    const atCap = (ethers.parseEther("0.04") * 10_000n) / 9_950n; // gross so that net = cap
    await (await launcher.connect(operator).launch(f, p, ethers.ZeroHash, SPEC, 0, { value: atCap - 10n ** 12n })).wait();
    expect(await factory.spentWei(await factory.allTokens(0), await launcher.getAddress())).to.be.lessThanOrEqual(ethers.parseEther("0.04"));
    // Rounding change from the curve is forwarded, never left behind.
    expect(await ethers.provider.getBalance(await launcher.getAddress())).to.equal(0n);
  });

  it("only the operator launches, and only with creator income routed to the splitter", async () => {
    const { operator, stranger, factory, weth, launcher, creator } = await setup();
    const w = await weth.getAddress();
    const f = await factory.getAddress();
    await expect(launcher.connect(stranger).launch(f, params(w, await creator.getAddress()), ethers.ZeroHash, SPEC, 0))
      .to.be.revertedWithCustomError(launcher, "NotOperator");
    await expect(launcher.connect(operator).launch(f, params(w, operator.address), ethers.ZeroHash, SPEC, 0))
      .to.be.revertedWithCustomError(launcher, "BadParams");
    await expect(launcher.connect(operator).launch(f, params(w, await creator.getAddress(), { founderSupplyBps: 500, vestingSecs: 86_400 }), ethers.ZeroHash, SPEC, 0))
      .to.be.revertedWithCustomError(launcher, "BadParams");
  });

  it("creator curve fees go launcher -> splitter -> tenant 90 / protocol 10", async () => {
    const { operator, tenant, protocol, trader, stranger, factory, weth, launcher, creator, platform } = await setup();
    const p = params(await weth.getAddress(), await creator.getAddress());
    await (await launcher.connect(operator).launch(await factory.getAddress(), p, ethers.ZeroHash, SPEC, 0)).wait();
    const coin = await factory.allTokens(0);
    await network.provider.send("evm_increaseTime", [61]);
    await network.provider.send("evm_mine");
    for (let i = 0; i < 4; i++) await (await factory.connect(trader).buy(coin, 0, { value: ethers.parseEther("0.05") })).wait();

    const accrued = await factory.feesAccrued(await launcher.getAddress());
    expect(accrued).to.be.greaterThan(0n);
    // The platform side accrues to the tenant's platform splitter, separately.
    expect(await factory.feesAccrued(await platform.getAddress())).to.be.greaterThan(0n);

    await (await launcher.connect(stranger).pull(await factory.getAddress())).wait();
    expect(await factory.feesAccrued(await launcher.getAddress())).to.equal(0n);
    expect(await creator.splittable(ethers.ZeroAddress)).to.equal(accrued);

    const t0 = await ethers.provider.getBalance(tenant.address);
    const p0 = await ethers.provider.getBalance(protocol.address);
    await (await creator.connect(stranger).split(ethers.ZeroAddress)).wait();
    const toProtocol = (accrued * PROTOCOL_BPS) / 10_000n;
    expect((await ethers.provider.getBalance(protocol.address)) - p0).to.equal(toProtocol);
    expect((await ethers.provider.getBalance(tenant.address)) - t0).to.equal(accrued - toProtocol);
  });

  it("pull with nothing accrued is a no-op", async () => {
    const { factory, launcher } = await setup();
    await expect(launcher.pull(await factory.getAddress())).to.not.emit(launcher, "Forwarded");
  });

  it("the hook's dev bucket is addressed to the creator splitter", async () => {
    const { operator, factory, weth, launcher, creator } = await setup();
    const p = params(await weth.getAddress(), await creator.getAddress());
    await (await launcher.connect(operator).launch(await factory.getAddress(), p, ethers.ZeroHash, SPEC, 0)).wait();
    const coin = await factory.allTokens(0);
    const policy = await factory.feePolicyOf(coin);
    expect(policy.devWallet).to.equal(await creator.getAddress());
  });
});
