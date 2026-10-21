import { expect } from "chai";
import { ethers } from "hardhat";

// AggregatorRouter: the terminal's router. It charges on the way in and trades
// through any pool, including launchpads we did not build.
//
// The V3 path is exercised end to end against a mock router; the V4 path shares
// _takeInput and _payOut with it, and its unlockCallback mirrors
// VentureRouter's, which venture.fork.test.ts covers against a live
// PoolManager. What is pinned here is the money: who pays the fee, in what, and
// that the contract keeps nothing.

const FEE_BPS = 100n; // 1%
const BPS = 10_000n;
/** Far enough ahead that no test is racing the clock. */
const LATER = 2n ** 48n;

function encodePath(tokenIn: string, fee: number, tokenOut: string): string {
  return ethers.concat([tokenIn, ethers.toBeHex(fee, 3), tokenOut]);
}

async function setup(feeBps: bigint = FEE_BPS) {
  const [deployer, trader, treasury, other] = await ethers.getSigners();
  const weth = await (await ethers.getContractFactory("WETH9")).deploy();
  const coin = await (await ethers.getContractFactory("MockERC20")).deploy();
  const v3 = await (await ethers.getContractFactory("MockV3Router")).deploy();
  const router = await (await ethers.getContractFactory("AggregatorRouter")).deploy(
    await weth.getAddress(), // poolManager: never called on the V3 path
    await v3.getAddress(),
    await weth.getAddress(),
    treasury.address,
    feeBps,
    ethers.ZeroAddress, // no syndicate: the fee is all the treasury's
    0,
  );
  return { deployer, trader, treasury, other, weth, coin, v3, router };
}

describe("AggregatorRouter", function () {
  this.timeout(300_000);

  it("refuses a bad configuration", async () => {
    const [, , treasury] = await ethers.getSigners();
    const weth = await (await ethers.getContractFactory("WETH9")).deploy();
    const w = await weth.getAddress();
    const F = await ethers.getContractFactory("AggregatorRouter");
    const Z = ethers.ZeroAddress;
    await expect(F.deploy(w, w, Z, treasury.address, 100, Z, 0)).to.be.revertedWithCustomError(F, "BadConfig");
    await expect(F.deploy(w, w, w, Z, 100, Z, 0)).to.be.revertedWithCustomError(F, "BadConfig");
    // Above the 1% ceiling.
    await expect(F.deploy(w, w, w, treasury.address, 101, Z, 0)).to.be.revertedWithCustomError(F, "BadConfig");
    // A rebate above the whole fee would pay out money never taken.
    await expect(F.deploy(w, w, w, treasury.address, 100, Z, 10_001)).to.be.revertedWithCustomError(F, "BadConfig");
  });

  it("caps the fee at 1% and quotes it before the trade", async () => {
    const { router } = await setup();
    expect(await router.MAX_FEE_BPS()).to.equal(100n);
    expect(await router.feeBps()).to.equal(FEE_BPS);
    const amount = ethers.parseEther("3");
    expect(await router.quoteFee(amount)).to.equal((amount * FEE_BPS) / BPS);
  });

  it("takes the fee from the input, in the input token, and keeps nothing", async () => {
    const { trader, treasury, weth, coin, v3, router } = await setup();
    const amountIn = ethers.parseEther("10");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("1000"))).wait();

    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    const fee = (amountIn * FEE_BPS) / BPS;
    const expectedOut = amountIn - fee; // the mock trades 1:1

    await expect(router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, LATER))
      .to.emit(router, "Routed")
      .withArgs(trader.address, await weth.getAddress(), await coin.getAddress(), amountIn, expectedOut, fee);

    expect(await weth.balanceOf(treasury.address)).to.equal(fee);
    expect(await coin.balanceOf(trader.address)).to.equal(expectedOut);
    // The router is a pass-through: nothing of either asset stays behind.
    expect(await weth.balanceOf(await router.getAddress())).to.equal(0n);
    expect(await coin.balanceOf(await router.getAddress())).to.equal(0n);
    expect(await ethers.provider.getBalance(await router.getAddress())).to.equal(0n);
  });

  it("wraps native ETH in, and charges the fee on the value sent", async () => {
    const { trader, treasury, weth, coin, v3, router } = await setup();
    const value = ethers.parseEther("2");
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("1000"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    const fee = (value * FEE_BPS) / BPS;

    // amountIn is ignored when value is sent, so a wrong one cannot overcharge.
    await (await router.connect(trader).swapV3(path, ethers.parseEther("999"), 0, ethers.ZeroAddress, false, LATER, { value })).wait();
    expect(await weth.balanceOf(treasury.address)).to.equal(fee);
    expect(await coin.balanceOf(trader.address)).to.equal(value - fee);
  });

  it("rejects native ETH when the input token is not WETH", async () => {
    const { trader, coin, weth, router } = await setup();
    const path = encodePath(await coin.getAddress(), 3000, await weth.getAddress());
    await expect(
      router.connect(trader).swapV3(path, 0, 0, ethers.ZeroAddress, false, LATER, { value: ethers.parseEther("1") }),
    ).to.be.revertedWithCustomError(router, "WrongInputForEth");
  });

  it("unwraps WETH output to native ETH when asked", async () => {
    const { trader, coin, weth, v3, router } = await setup();
    const amountIn = ethers.parseEther("5");
    await (await coin.mint(trader.address, amountIn)).wait();
    await (await coin.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    // Fund the mock with WETH to pay out.
    await (await weth.deposit({ value: ethers.parseEther("100") })).wait();
    await (await weth.transfer(await v3.getAddress(), ethers.parseEther("100"))).wait();

    const path = encodePath(await coin.getAddress(), 3000, await weth.getAddress());
    const fee = (amountIn * FEE_BPS) / BPS;
    const before = await ethers.provider.getBalance(trader.address);
    const rc = await (await router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, true, LATER)).wait();
    const gas = rc!.gasUsed * rc!.gasPrice;
    expect((await ethers.provider.getBalance(trader.address)) - before + gas).to.equal(amountIn - fee);
    // The fee stays in WETH; only the trader's proceeds are unwrapped.
    expect(await coin.balanceOf(await router.getAddress())).to.equal(0n);
  });

  it("pays a named recipient instead of the caller", async () => {
    const { trader, other, weth, coin, v3, router } = await setup();
    const amountIn = ethers.parseEther("1");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("10"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    await (await router.connect(trader).swapV3(path, amountIn, 0, other.address, false, LATER)).wait();
    expect(await coin.balanceOf(other.address)).to.be.greaterThan(0n);
    expect(await coin.balanceOf(trader.address)).to.equal(0n);
  });

  it("enforces its own slippage floor rather than trusting the pool's", async () => {
    const { trader, weth, coin, v3, router } = await setup();
    const amountIn = ethers.parseEther("4");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("100"))).wait();
    // Half the expected output.
    await (await v3.setRate(ethers.parseEther("0.5"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    await expect(
      router.connect(trader).swapV3(path, amountIn, ethers.parseEther("3"), ethers.ZeroAddress, false, LATER),
    ).to.be.revertedWithCustomError(router, "Slippage");
  });

  it("refuses an empty input or a path too short to name two tokens", async () => {
    const { trader, weth, coin, router } = await setup();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    await expect(router.connect(trader).swapV3(path, 0, 0, ethers.ZeroAddress, false, LATER)).to.be.revertedWithCustomError(router, "NoInput");
    await expect(
      router.connect(trader).swapV3(await weth.getAddress(), 1, 0, ethers.ZeroAddress, false, LATER),
    ).to.be.revertedWithCustomError(router, "BadConfig");
  });

  it("routes a 1 wei trade, whose fee rounds to nothing", async () => {
    const { trader, treasury, weth, coin, v3, router } = await setup();
    await (await weth.connect(trader).deposit({ value: 1n })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), 1n)).wait();
    await (await coin.mint(await v3.getAddress(), 1_000n)).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    // The 1% ceiling means a fee can never consume the whole input: at 1 wei it
    // floors to 0 and the trade goes through uncharged. The `netIn == 0` guard
    // in _takeInput is therefore unreachable while MAX_FEE_BPS stays at 100;
    // it is kept so raising the cap could not silently swap for nothing.
    await (await router.connect(trader).swapV3(path, 1n, 0, ethers.ZeroAddress, false, LATER)).wait();
    expect(await weth.balanceOf(treasury.address)).to.equal(0n);
    expect(await coin.balanceOf(trader.address)).to.equal(1n);
  });

  it("charges nothing at a zero fee, and still routes", async () => {
    const { trader, treasury, weth, coin, v3, router } = await setup(0n);
    const amountIn = ethers.parseEther("1");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("10"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    await (await router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, LATER)).wait();
    expect(await weth.balanceOf(treasury.address)).to.equal(0n);
    expect(await coin.balanceOf(trader.address)).to.equal(amountIn);
  });

  it("only the pool manager can drive the unlock callback", async () => {
    const { router } = await setup();
    await expect(router.unlockCallback("0x")).to.be.revertedWithCustomError(router, "NotPoolManager");
  });

  it("refuses a trade whose deadline has passed", async () => {
    const { router, weth, coin, v3, trader } = await setup();
    const amountIn = ethers.parseEther("1");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("10"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());

    const now = BigInt((await ethers.provider.getBlock("latest"))!.timestamp);
    await expect(
      router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, now - 1n),
    ).to.be.revertedWithCustomError(router, "Expired");
    // And nothing was taken on the way: a refused trade costs no fee.
    expect(await weth.balanceOf(trader.address)).to.equal(amountIn);
  });

  it("treats a missing deadline as expired, not as no deadline", async () => {
    // A caller who forgets the field should be refused rather than silently
    // given a trade with no staleness protection at all.
    const { router, weth, coin, v3, trader } = await setup();
    await (await weth.connect(trader).deposit({ value: ethers.parseEther("1") })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), ethers.parseEther("1"))).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("10"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());
    await expect(
      router.connect(trader).swapV3(path, ethers.parseEther("1"), 0, ethers.ZeroAddress, false, 0),
    ).to.be.revertedWithCustomError(router, "Expired");
  });

  it("accepts a deadline in this very block", async () => {
    // The boundary is inclusive: a trade signed for "now" must still land.
    const { router, weth, coin, v3, trader } = await setup();
    const amountIn = ethers.parseEther("1");
    await (await weth.connect(trader).deposit({ value: amountIn })).wait();
    await (await weth.connect(trader).approve(await router.getAddress(), amountIn)).wait();
    await (await coin.mint(await v3.getAddress(), ethers.parseEther("10"))).wait();
    const path = encodePath(await weth.getAddress(), 3000, await coin.getAddress());

    const next = BigInt((await ethers.provider.getBlock("latest"))!.timestamp) + 1n;
    await ethers.provider.send("evm_setNextBlockTimestamp", [Number(next)]);
    await (await router.connect(trader).swapV3(path, amountIn, 0, ethers.ZeroAddress, false, next)).wait();
    expect(await coin.balanceOf(trader.address)).to.be.greaterThan(0n);
  });

  it("every swap entry point takes a deadline", async () => {
    // A path that forgot one would be the one an attacker uses.
    const { router } = await setup();
    for (const name of ["swapV3", "swapV4"]) {
      const fn = router.interface.getFunction(name as "swapV3");
      expect(fn.inputs[fn.inputs.length - 1]!.name, name).to.equal("deadline");
    }
  });

  it("has no admin: the treasury and the rate are immutable", async () => {
    const { router } = await setup();
    const fns = router.interface.fragments.filter((f) => f.type === "function").map((f) => (f as unknown as { name: string }).name);
    for (const forbidden of ["setTreasury", "setFeeBps", "withdraw", "rescue", "owner", "transferOwnership"]) {
      expect(fns).to.not.include(forbidden);
    }
  });
});
