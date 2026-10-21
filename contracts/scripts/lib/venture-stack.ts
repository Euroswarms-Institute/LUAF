import { ethers, network } from "hardhat";
import { writeFileSync, mkdirSync } from "fs";
import { join } from "path";

// Venture launchpad stack deploy, shared by scripts/deploy-venture.ts (the
// doubleplus stack) and scripts/deploy-tenant.ts (a tenant stack whose
// treasury is a FeeSplitter). Serves both Robinhood Chain
// networks through the env-driven `robinhood` network entry:
//
//   mainnet: ROBINHOOD_RPC_URL=https://rpc.mainnet.chain.robinhood.com ROBINHOOD_CHAIN_ID=4663
//   testnet: ROBINHOOD_RPC_URL=https://rpc.testnet.chain.robinhood.com ROBINHOOD_CHAIN_ID=46630
//
// Env:
//   ADMIN            protocolAdmin: pause/resume and setParams only; it has no
//                    power over pools (default: deployer)
//   TREASURY         protocol fee treasury baked into the hook (default: ADMIN)
//   PLATFORM_FEE_BPS protocol fee on every trade, 50..100 (default 55 = 0.55%)
//   REF_SHARE_BPS    referrer's cut of the protocol fee, 0..5000 (default 2000 = 20%)
//   ETH_USD_MIN/MAX  accepted band for the launcher-supplied ETH/USD price, whole
//                    dollars (mainnet default 1000..10000, see ETH_USD_BAND)
//   MIN_TARGET_ETH   smallest raise the platform finishes, immutable (default 2)
//   GRADUATION_RAISE_ETH  open-curve graduation trigger (contract default 4)
//   CREATION_FEE_ETH flat launch fee (default 0)
//                    The last two are setParams values. They are applied here
//                    when the deployer is ADMIN; otherwise the call is printed
//                    for the admin to send. setParams state never survives a
//                    redeploy, so this is the step that keeps them.
//
// The V4 PoolManager sits at the same address on both networks. WETH differs;
// the testnet has no self-deployed V3 stack, so venture launches there are
// WETH-paired only (v3Router = zero, never called for WETH pairs).
export const INFRA: Record<number, { poolManager: string; weth: string; v3Router: string; file: string }> = {
  4663: {
    poolManager: "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    weth: "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
    v3Router: "0xCaf681a66D020601342297493863E78C959E5cb2",
    file: "venture-robinhood.json",
  },
  46630: {
    poolManager: "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    weth: "0x33e4191705c386532ba27cBF171Db86919200B94", // canonical WETH9 (verified, bridge-wrapped)
    v3Router: ethers.ZeroAddress,
    file: "venture-testnet.json",
  },
};

// Default band for the ETH/USD price a launcher passes to launch(), in whole
// dollars. Immutable on the factory. Mainnet is a band around the live price:
// wide enough to survive the market for the factory's life, narrow enough that
// a launcher cannot open their own curve near zero. The testnet fallback price
// is deliberately scaled up (~$1.86M) so faucet wallets can graduate a raise,
// so the testnet band is wide.
export const ETH_USD_BAND: Record<number, [number, number]> = {
  4663: [1_000, 10_000],
  46630: [1, 10_000_000],
};

// beforeInitialize | afterSwap | afterSwapReturnDelta
const HOOK_FLAGS = (1n << 13n) | (1n << 6n) | (1n << 2n);
const FLAG_MASK = (1n << 14n) - 1n;

export interface StackOptions {
  /** Overrides TREASURY: the address baked into the hook as platformTreasury. */
  treasury?: string;
  /** Overrides the per-chain deployment file name under deployments/. */
  file?: string;
  /** Extra top-level fields written into the deployment JSON. */
  extra?: Record<string, unknown>;
}

export async function deployVentureStack(opts: StackOptions = {}) {
  const chainId = Number((await ethers.provider.getNetwork()).chainId);
  const infra = INFRA[chainId];
  if (!infra) throw new Error(`no venture infra config for chain ${chainId}`);

  const [signer] = await ethers.getSigners();
  const admin = process.env.ADMIN ?? signer.address;
  const treasury = opts.treasury ?? process.env.TREASURY ?? admin;
  const platformFeeBps = Number(process.env.PLATFORM_FEE_BPS ?? 55);
  const refShareBps = Number(process.env.REF_SHARE_BPS ?? 2000);
  // Curve-phase protocol fees. Immutable once deployed, so they are set here.
  const curveBuyFeeBps = Number(process.env.CURVE_BUY_FEE_BPS ?? 50);
  const curveSellFeeBps = Number(process.env.CURVE_SELL_FEE_BPS ?? 100);
  // Smallest raise the platform will finish. Immutable on the factory, so it
  // is a deploy-time decision: mainnet ships 2 ETH (a floor-sized raise
  // graduates near 6 ETH FDV), testnets override it down so a raise can
  // actually be driven to graduation.
  const minTargetWei = ethers.parseEther(process.env.MIN_TARGET_ETH ?? "2");
  // The address mark, matched at either end: 0x2add… or 0x…2add. Immutable, so
  // it is a deploy-time decision; 0 would disable it and is for tests only.
  const VANITY = 0x2add;
  const [bandMin, bandMax] = ETH_USD_BAND[chainId];
  const minEthUsd8 = BigInt(process.env.ETH_USD_MIN ?? bandMin) * 10n ** 8n;
  const maxEthUsd8 = BigInt(process.env.ETH_USD_MAX ?? bandMax) * 10n ** 8n;
  console.log(`network: ${network.name} (${chainId})  deployer: ${signer.address}`);
  console.log(`admin: ${admin}  treasury: ${treasury}  platformFeeBps: ${platformFeeBps}  refShareBps: ${refShareBps}`);
  console.log(`curveBuyFeeBps: ${curveBuyFeeBps}  curveSellFeeBps: ${curveSellFeeBps}`);
  console.log(`minTargetWei: ${ethers.formatEther(minTargetWei)} ETH  vanity: 0x${VANITY.toString(16)}`);
  console.log(`ETH/USD band: $${minEthUsd8 / 10n ** 8n} .. $${maxEthUsd8 / 10n ** 8n}`);

  // 1) CREATE2 deployer + vesting deployer, then pin the factory address two
  //    creates ahead so the hook (immutable launcher) and the token deployer
  //    can both bake it in before the factory exists.
  const c2 = await (await ethers.getContractFactory("HookDeployer")).deploy();
  await c2.waitForDeployment();
  const c2Addr = await c2.getAddress();

  const vestingDeployer = await (await ethers.getContractFactory("VestingDeployer")).deploy();
  await vestingDeployer.waitForDeployment();

  const nonce = await ethers.provider.getTransactionCount(signer.address);
  // nonce n+0: hook deploy tx (CREATE2 via c2 — does not consume a signer create)
  // ...but it IS a tx from the signer, so plain creates land at n+1 and n+2.
  const predictedFactory = ethers.getCreateAddress({ from: signer.address, nonce: nonce + 2 });

  // 2) Mine + deploy the hook at a flag-matching address, launcher pre-baked.
  const Hook = await ethers.getContractFactory("VentureFeeHook");
  const hookArgs = ethers.AbiCoder.defaultAbiCoder().encode(
    ["address", "address", "address", "uint16", "uint16"],
    [infra.poolManager, treasury, predictedFactory, platformFeeBps, refShareBps],
  );
  const hookInit = ethers.concat([Hook.bytecode, hookArgs]);
  const hookHash = ethers.keccak256(hookInit);
  let hookAddr = "", salt = "";
  for (let i = 0n; i < 4_000_000n; i++) {
    const s = ethers.zeroPadValue(ethers.toBeHex(i), 32);
    const a = ethers.getCreate2Address(c2Addr, s, hookHash);
    if ((BigInt(a) & FLAG_MASK) === HOOK_FLAGS) { hookAddr = a; salt = s; break; }
  }
  if (!hookAddr) throw new Error("no hook salt");
  await (await c2.deploy(salt, hookInit)).wait();
  console.log("hook:", hookAddr);

  // 3) Token deployer bound to the predicted factory, then the factory itself.
  const tokenDeployer = await (await ethers.getContractFactory("VentureTokenDeployer")).deploy(predictedFactory);
  await tokenDeployer.waitForDeployment();

  const factory = await (await ethers.getContractFactory("VentureFactory")).deploy(
    signer.address, admin, infra.poolManager, hookAddr, infra.weth, infra.v3Router,
    await vestingDeployer.getAddress(), await tokenDeployer.getAddress(),
    curveBuyFeeBps, curveSellFeeBps, minTargetWei, VANITY, minEthUsd8, maxEthUsd8,
  );
  await factory.waitForDeployment();
  const factoryAddr = await factory.getAddress();
  if (factoryAddr !== predictedFactory) throw new Error(`factory address drift: ${factoryAddr} != ${predictedFactory}`);
  console.log("factory:", factoryAddr);

  const router = await (await ethers.getContractFactory("VentureRouter")).deploy(
    infra.poolManager, factoryAddr, infra.weth, infra.v3Router,
  );
  await router.waitForDeployment();
  const routerAddr = await router.getAddress();
  console.log("router:", routerAddr);

  const updates = await (await ethers.getContractFactory("VentureUpdates")).deploy(factoryAddr);
  await updates.waitForDeployment();
  console.log("updates:", await updates.getAddress());

  // setParams: open-curve graduation trigger and launch fee.
  const gradWei = ethers.parseEther(process.env.GRADUATION_RAISE_ETH ?? "4");
  const creationFeeWei = ethers.parseEther(process.env.CREATION_FEE_ETH ?? "0");
  const sweepDelaySecs = 365 * 86_400, creatorCurveShareBps = 1_000;
  if (admin.toLowerCase() === signer.address.toLowerCase()) {
    await (await factory.setParams(creationFeeWei, gradWei, sweepDelaySecs, creatorCurveShareBps)).wait();
  } else {
    console.log("ADMIN must now send:", `setParams(${creationFeeWei}, ${gradWei}, ${sweepDelaySecs}, ${creatorCurveShareBps})`, "to", predictedFactory);
  }
  console.log(`graduationRaiseWei: ${ethers.formatEther(await factory.graduationRaiseWei())} ETH  creationFeeWei: ${ethers.formatEther(await factory.creationFeeWei())} ETH`);

  await (await factory.renounceOwnership()).wait();
  console.log("factory ownership renounced (hook has no owner by construction)");

  const startBlock = await ethers.provider.getBlockNumber();
  const out = {
    chainId,
    ...opts.extra,
    admin,
    treasury,
    platformFeeBps,
    refShareBps,
    curveBuyFeeBps,
    curveSellFeeBps,
    minTargetWei: minTargetWei.toString(),
    vanity: `0x${VANITY.toString(16)}`,
    ethUsdBand8: [minEthUsd8.toString(), maxEthUsd8.toString()],
    startBlock,
    contracts: {
      hookDeployer: c2Addr,
      hook: hookAddr,
      factory: factoryAddr,
      router: routerAddr,
      updates: await updates.getAddress(),
      tokenDeployer: await tokenDeployer.getAddress(),
      vestingDeployer: await vestingDeployer.getAddress(),
      poolManager: infra.poolManager,
      weth: infra.weth,
      v3Router: infra.v3Router,
    },
  };
  const file = opts.file ?? infra.file;
  mkdirSync(join(__dirname, "../../deployments"), { recursive: true });
  writeFileSync(join(__dirname, `../../deployments/${file}`), JSON.stringify(out, null, 2));
  console.log(`saved deployments/${file}  startBlock:`, startBlock);
  return out;
}
