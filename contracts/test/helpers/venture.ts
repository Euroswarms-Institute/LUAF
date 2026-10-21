/** Vendored from DoublePlus contracts/test/v4/helpers/venture.ts. Do not edit
 *  here; re-run scripts/sync-doubleplus.sh. It deploys the real venture stack,
 *  which is what makes the suites below integration tests of the contracts the
 *  Launcher and the syndicate will actually meet rather than of mocks. */

import { expect } from "chai";
import { ethers, network } from "hardhat";

/** The mark every real deployment ships: "2-add", double plus. */
export const VANITY = 0x2add;

/** Tests deploy with the mark disabled, so no salt has to be mined at all.
 *  A "cheaper" mark would not work: any non-zero value is still a 16-bit
 *  compare, so 0x000f costs exactly what 0x2add does. The factory takes the
 *  mark as a constructor immutable precisely so the suite can switch it off. */
export const TEST_VANITY = 0;

/** Does this address carry `mark` at either end?
 *
 *  Both ends count: two targets at the same odds halves the client's search,
 *  and truncated displays (0x2add…9C6f / 0xcD04…2add) show both ends anyway.
 *
 *  Case: EIP-55 casing is derived off-chain from the address hash, so the
 *  contract cannot enforce it. Require all-lower or all-upper here so no token
 *  ever reads 0x…2AdD. Compared as strings — a slice beats parsing 40 hex
 *  characters into a BigInt, and it carries the checksum case for free. */
export function isVanity(addr: string, mark: number = VANITY): boolean {
  if (mark === 0) return true; // mark disabled (tests)
  const lo = mark.toString(16).padStart(4, "0");
  const hi = lo.toUpperCase();
  const head = addr.slice(2, 6);
  if (head === lo || head === hi) return true;
  const tail = addr.slice(-4);
  return tail === lo || tail === hi;
}

// Shared fixtures for the venture suites. Kept in one place so the stress
// runs exercise exactly the stack the unit tests do — a second, drifting copy
// of deployStack would quietly test a different contract.

export const ETH_USD_8 = 1865n * 10n ** 8n;
/** The mainnet band the suites deploy with, so tests exercise the real guard. */
export const ETH_USD_MIN_8 = 1_000n * 10n ** 8n;
export const ETH_USD_MAX_8 = 10_000n * 10n ** 8n;
export const TARGET = ethers.parseEther("2");
export const DAY = 86_400;

export async function deployStack(
  minTargetWei: bigint = 1n,
  ethUsdBand8: [bigint, bigint] = [ETH_USD_MIN_8, ETH_USD_MAX_8],
  treasury?: string,
) {
  const [admin] = await ethers.getSigners();
  const weth = await (await ethers.getContractFactory("WETH9")).deploy();
  await weth.waitForDeployment();

  // poolManager/v3Router are inert during the curve phase; any code-bearing
  // address that is never called works for these tests. The hook is real so
  // launch() can read its MAX_SIDE_TAX_BPS guardrail (its flags are only
  // checked by the PoolManager, which the curve phase never touches).
  const placeholder = await weth.getAddress();

  const vestingDeployer = await (await ethers.getContractFactory("VestingDeployer")).deploy();
  await vestingDeployer.waitForDeployment();

  const nonce = await ethers.provider.getTransactionCount(admin.address);
  const predictedFactory = ethers.getCreateAddress({ from: admin.address, nonce: nonce + 2 });
  const hook = await (await ethers.getContractFactory("VentureFeeHook")).deploy(
    placeholder, treasury ?? admin.address, predictedFactory, 100, 2000,
  );
  await hook.waitForDeployment();
  const tokenDeployer = await (await ethers.getContractFactory("VentureTokenDeployer")).deploy(predictedFactory);
  await tokenDeployer.waitForDeployment();

  const factory = await (await ethers.getContractFactory("VentureFactory")).deploy(
    admin.address,
    admin.address,
    placeholder, // poolManager (unused pre-finalize)
    await hook.getAddress(),
    await weth.getAddress(),
    placeholder, // v3Router (unused when pair is WETH)
    await vestingDeployer.getAddress(),
    await tokenDeployer.getAddress(),
     50, 100,
    minTargetWei,
    TEST_VANITY, // mark off: the suite mines no salts
    ethUsdBand8[0],
    ethUsdBand8[1],
  );
  await factory.waitForDeployment();
  expect(await factory.getAddress()).to.equal(predictedFactory);
  return { factory, tokenDeployer, weth, hook };
}

export async function mineSalt(tokenDeployer: any, args: any[]) {
  const Token = await ethers.getContractFactory("QuiverToken");
  const encoded = ethers.AbiCoder.defaultAbiCoder().encode(
    ["string", "string", "string", "uint256", "address", "address", "uint16", "address", "uint256", "uint8"],
    args,
  );
  // With the mark disabled any salt is valid, so skip the grind entirely.
  if (TEST_VANITY === 0) return ethers.ZeroHash;
  const hash = ethers.keccak256(ethers.concat([Token.bytecode, encoded]));
  const depAddr = await tokenDeployer.getAddress();
  for (let i = 0n; i < 6_000_000n; i++) {
    const s = ethers.zeroPadValue(ethers.toBeHex(i), 32);
    if (isVanity(ethers.getCreate2Address(depAddr, s, hash), TEST_VANITY)) return s;
  }
  throw new Error("no vanity salt");
}

export async function launch(
  factory: any,
  tokenDeployer: any,
  creator: any,
  weth: string,
  overrides: Partial<Record<string, any>> = {},
  value: bigint = 0n,
  expectRevert = false,
) {
  const params = {
    name: "Venture",
    symbol: "VNT",
    metadataURI: "",
    pair: weth,
    buyTaxBps: 300,
    sellTaxBps: 300,
    devWallet: "0x0000000000000000000000000000000000000000",
    devBps: 2500,
    dividendBps: 2500,
    liquidityBps: 2500,
    mmBps: 2500,
    ethUsdPrice8: ETH_USD_8,
    targetRaiseWei: TARGET,
    raiseDurationSecs: 2 * DAY,
    maxBuyWei: TARGET, // no cap unless the test sets one
    founderRaiseBps: 3000,
    founderSupplyBps: 1000,
    vestingSecs: 180 * DAY,
    mode: 0,
    minHoldForDividends: 0,
    dividendMode: 0,
    v3Path: "0x",
    ...overrides,
  };
  delete (params as Record<string, unknown>).inLaunchWindow;
  // Every InvalidParams guard in launch() runs before deployToken(), so a call
  // expected to revert on its terms never reaches the vanity check and does not
  // need a mined salt. Mining one costs ~65k keccaks in JS, which is most of the
  // runtime of any test that asserts a batch of rejections.
  if (expectRevert) {
    return factory.connect(creator).launch(params, ethers.ZeroHash, { value });
  }
  const salt = await mineSalt(tokenDeployer, [
    params.name,
    params.symbol,
    params.metadataURI,
    10n ** 27n,
    creator.address,
    await factory.getAddress(),
    params.buyTaxBps,
    params.pair,
    BigInt(params.minHoldForDividends) * 10n ** 18n, // the deployer scales it
    params.dividendMode,
  ]);
  await (await factory.connect(creator).launch(params, salt, { value })).wait();
  // Step past the launch window, whose tighter per-wallet cap would otherwise
  // bind every buy these suites make. The window has its own test.
  if (!overrides.inLaunchWindow) {
    await network.provider.send("evm_increaseTime", [61]);
    await network.provider.send("evm_mine");
  }
  return factory.allTokens((await factory.totalTokens()) - 1n);
}
