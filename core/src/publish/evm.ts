/** Launch a designed agent as a coin on a tenant's venture factory, through
 *  our Launcher contract.
 *
 *  This replaces the Python pipeline's two targets. The marketplace POST and
 *  the Solana tokenization are gone: both published onto someone else's ground
 *  and paid rent there, and the Solana path required handing a raw wallet key
 *  to a remote API. Here the coin is created in a stack we hold, the Launcher
 *  is its creator, and the creator income is split by contract.
 *
 *  Addresses carry the stack's mark (0x2add on the shipped deployments), which
 *  the factory enforces, so a salt has to be mined before the launch. The
 *  search mirrors `web/src/venture/Launch.tsx`: predict the CREATE2 address of
 *  QuiverToken from the token deployer, match the mark at either end, and start
 *  from a random offset so a retried launch does not re-mine a taken address. */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import {
  createPublicClient,
  createWalletClient,
  encodeAbiParameters,
  getContractAddress,
  http,
  keccak256,
  concatHex,
  parseAbi,
  defineChain,
  type Address,
  type Hex,
  type PublicClient,
  type WalletClient,
} from "viem";
import { privateKeyToAccount } from "viem/accounts";

import type { LauncherDeployment, RunConfig } from "../config.js";
import { RAISE_OPEN, validateSpec, specId as computeSpecId, type LaunchSpec } from "../spec.js";
import type { Product } from "../payload.js";

export class PublishError extends Error {}

/** The whole supply every venture coin is deployed with. */
const TOTAL_SUPPLY = 1_000_000_000n * 10n ** 18n;
/** The factory stores metadata on-chain in the launch transaction. */
const METADATA_MAX_BYTES = 18_000;
/** In the first 60 s a wallet may put at most 1/100 of the target into the curve. */
const LAUNCH_CAP_DIV = 100n;

export const launcherAbi = parseAbi([
  "struct LaunchParams { string name; string symbol; string metadataURI; address pair; uint16 buyTaxBps; uint16 sellTaxBps; address devWallet; uint16 devBps; uint16 dividendBps; uint16 liquidityBps; uint16 mmBps; uint256 ethUsdPrice8; uint256 targetRaiseWei; uint64 raiseDurationSecs; uint256 maxBuyWei; uint16 founderRaiseBps; uint16 founderSupplyBps; uint32 vestingSecs; uint8 mode; uint256 minHoldForDividends; uint8 dividendMode; bytes v3Path; }",
  "function launch(address factory, LaunchParams p, bytes32 salt, bytes32 specId, uint256 minTokensOut) payable returns (address token, uint256 tokensOut)",
  "function pull(address factory) returns (uint256)",
  "function operator() view returns (address)",
  "function splitter() view returns (address)",
  "event Launched(bytes32 indexed specId, address indexed token, address indexed factory, uint256 firstBuyWei, uint256 tokensOut)",
]);

export const factoryAbi = parseAbi([
  "function creationFeeWei() view returns (uint256)",
  "function graduationRaiseWei() view returns (uint256)",
  "function minEthUsd8() view returns (uint64)",
  "function maxEthUsd8() view returns (uint64)",
  "function launchesPaused() view returns (bool)",
  "function totalTokens() view returns (uint256)",
  "function feesAccrued(address) view returns (uint256)",
]);

/** The QuiverToken creation bytecode, needed to predict a coin's address.
 *  Written into the launcher deployment JSON at deploy time so a release does
 *  not depend on build artifacts; the artifact and the environment are
 *  fallbacks for local work. It must match the bytecode the deployed token
 *  deployer uses, or every mined address will be wrong. */
export function tokenBytecode(dep: LauncherDeployment & { tokenBytecode?: Hex }): Hex {
  const fromEnv = process.env.LUAF_TOKEN_BYTECODE?.trim();
  if (fromEnv) return fromEnv as Hex;
  if (dep.tokenBytecode) return dep.tokenBytecode;
  const artifact = join(
    process.cwd(),
    "contracts/artifacts/contracts/vendor/doubleplus/v4/QuiverToken.sol/QuiverToken.json",
  );
  if (existsSync(artifact)) {
    const json = JSON.parse(readFileSync(artifact, "utf8")) as { bytecode?: Hex };
    if (json.bytecode && json.bytecode !== "0x") return json.bytecode;
  }
  throw new PublishError(
    "no QuiverToken bytecode: add tokenBytecode to the launcher deployment, set LUAF_TOKEN_BYTECODE, " +
      "or compile contracts/ first",
  );
}

/** Does `addr` carry `mark` at either end? Both ends count, which halves the
 *  search; the case must be uniform so no address ever reads 0x…2AdD. */
export function isVanity(addr: string, mark: number): boolean {
  if (!mark) return true;
  const lo = mark.toString(16).padStart(4, "0");
  const hi = lo.toUpperCase();
  const head = addr.slice(2, 6);
  if (head === lo || head === hi) return true;
  const tail = addr.slice(-4);
  return tail === lo || tail === hi;
}

export interface MineRequest {
  tokenDeployer: Address;
  factory: Address;
  /** The address the factory will see as msg.sender: our Launcher. */
  creator: Address;
  bytecode: Hex;
  name: string;
  symbol: string;
  metadataURI: string;
  pair: Address;
  buyTaxBps: number;
  minHoldForDividends: number;
  dividendMode: number;
  mark: number;
  /** Candidates to try before giving up. */
  budget?: number;
}

/** Mine a CREATE2 salt whose predicted coin address carries the mark. */
export function mineSalt(req: MineRequest): Hex {
  if (!req.mark) return `0x${"0".repeat(64)}`;
  const args = encodeAbiParameters(
    [
      { type: "string" },
      { type: "string" },
      { type: "string" },
      { type: "uint256" },
      { type: "address" },
      { type: "address" },
      { type: "uint16" },
      { type: "address" },
      { type: "uint256" },
      { type: "uint8" },
    ],
    [
      req.name,
      req.symbol,
      req.metadataURI,
      TOTAL_SUPPLY,
      req.creator,
      req.factory,
      req.buyTaxBps,
      req.pair,
      // The deployer scales the whole-token floor, so the salt must be mined
      // against the value the constructor actually receives.
      BigInt(req.minHoldForDividends) * 10n ** 18n,
      req.dividendMode,
    ],
  );
  const bytecodeHash = keccak256(concatHex([req.bytecode, args]));
  const budget = BigInt(req.budget ?? 3_000_000);
  // Start at a random offset: a retry after a dropped receipt would otherwise
  // mine the same salt, land on a taken address and revert.
  const start = BigInt(`0x${Buffer.from(crypto.getRandomValues(new Uint8Array(8))).toString("hex")}`) << 32n;
  for (let i = 0n; i < budget; i++) {
    const salt = `0x${(start + i).toString(16).padStart(64, "0")}` as Hex;
    const addr = getContractAddress({ opcode: "CREATE2", from: req.tokenDeployer, salt, bytecodeHash });
    if (isVanity(addr, req.mark)) return salt;
  }
  throw new PublishError(`no salt carrying 0x${req.mark.toString(16)} in ${budget} candidates`);
}

/** The on-chain metadata blob. Same shape the web app writes, so a coin
 *  launched by the pipeline renders identically in the brand front ends. */
export function buildMetadata(product: Product, extra: Record<string, string> = {}): string {
  const meta = {
    description: product.description,
    pitch: product.description.split(/(?<=[.!?])\s/)[0] ?? product.description,
    sector: product.tags.split(",")[0]?.trim() ?? "",
    tags: product.tags,
    useCases: product.useCases.slice(0, 5),
    ...extra,
  };
  const json = JSON.stringify(meta);
  if (Buffer.byteLength(json, "utf8") <= METADATA_MAX_BYTES) return json;
  // Too large to store: drop the use cases, then truncate the description.
  const lean = { ...meta, useCases: [] as typeof meta.useCases };
  let text = JSON.stringify(lean);
  if (Buffer.byteLength(text, "utf8") > METADATA_MAX_BYTES) {
    lean.description = lean.description.slice(0, 2_000);
    text = JSON.stringify(lean);
  }
  if (Buffer.byteLength(text, "utf8") > METADATA_MAX_BYTES) {
    throw new PublishError("metadata does not fit on-chain even after trimming");
  }
  return text;
}

function chainFor(dep: LauncherDeployment, rpcUrl: string) {
  return defineChain({
    id: dep.chainId,
    name: `chain-${dep.chainId}`,
    nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
    rpcUrls: { default: { http: [rpcUrl] } },
  });
}

export interface PublishRequest {
  spec: LaunchSpec;
  product: Product;
  dep: LauncherDeployment & { tokenBytecode?: Hex };
  run: RunConfig;
  /** ETH/USD with 8 decimals. The factory only accepts a price inside its
   *  deployed band, which sizes the curve's start price. */
  ethUsd8: bigint;
}

export interface PublishResult {
  /** True when nothing was signed. */
  dryRun: boolean;
  specId: Hex;
  salt: Hex;
  /** Predicted before the launch; the real one after. */
  token?: Address;
  txHash?: Hex;
  firstBuyWei: bigint;
  tokensOut?: bigint;
  metadataBytes: number;
}

/** Design -> coin. Validates the spec and the factory's preconditions, mines a
 *  salt, then launches (unless this is a dry run). */
export async function publish(req: PublishRequest): Promise<PublishResult> {
  const { spec, product, dep, run } = req;
  validateSpec(spec);
  if (spec.mode !== RAISE_OPEN) throw new PublishError("the Launcher only opens Open-mode curves");
  if (spec.chainId !== dep.chainId) {
    throw new PublishError(`spec is for chain ${spec.chainId}, deployment is for ${dep.chainId}`);
  }

  const chain = chainFor(dep, run.rpcUrl);
  const publicClient = createPublicClient({ chain, transport: http(run.rpcUrl) }) as PublicClient;
  const net = await publicClient.getChainId();
  if (net !== dep.chainId) {
    throw new PublishError(`RPC serves chain ${net}, deployment is for ${dep.chainId}`);
  }

  const [creationFeeWei, graduationRaiseWei, minEthUsd8, maxEthUsd8, paused] = await Promise.all([
    publicClient.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "creationFeeWei" }),
    publicClient.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "graduationRaiseWei" }),
    publicClient.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "minEthUsd8" }),
    publicClient.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "maxEthUsd8" }),
    publicClient.readContract({ address: dep.contracts.factory, abi: factoryAbi, functionName: "launchesPaused" }),
  ]);
  if (paused) throw new PublishError("the factory has launches paused");
  if (req.ethUsd8 < minEthUsd8 || req.ethUsd8 > maxEthUsd8) {
    throw new PublishError(
      `ETH/USD ${req.ethUsd8} is outside the factory's band ${minEthUsd8}..${maxEthUsd8}; the launch would revert`,
    );
  }

  // The first buy has to fit the launch-window cap or the whole launch
  // reverts, so clamp rather than fail: the spec asked for an opening buy, not
  // for a specific one. The cap applies to what reaches the curve, and the
  // entry fee comes off the top, so leave a little room for it.
  const windowCap = graduationRaiseWei / LAUNCH_CAP_DIV;
  const headroom = (windowCap * 99n) / 100n;
  const firstBuyWei = spec.firstBuyWei > headroom ? headroom : spec.firstBuyWei;

  const metadataURI = buildMetadata(product);
  const mark = dep.vanity ? Number(BigInt(dep.vanity)) : 0;
  const salt = mineSalt({
    tokenDeployer: dep.contracts.tokenDeployer,
    factory: dep.contracts.factory,
    creator: dep.contracts.launcher,
    bytecode: tokenBytecode(dep),
    name: product.name,
    symbol: product.ticker,
    metadataURI,
    pair: dep.contracts.weth,
    buyTaxBps: spec.tax.buyTaxBps,
    minHoldForDividends: spec.minHoldForDividends,
    dividendMode: spec.dividendMode,
    mark,
  });

  const params = {
    name: product.name,
    symbol: product.ticker,
    metadataURI,
    pair: dep.contracts.weth,
    buyTaxBps: spec.tax.buyTaxBps,
    sellTaxBps: spec.tax.sellTaxBps,
    // The Launcher rejects anything else: creator income must be splittable.
    devWallet: dep.contracts.creatorSplitter,
    devBps: spec.tax.devBps,
    dividendBps: spec.tax.dividendBps,
    liquidityBps: spec.tax.liquidityBps,
    mmBps: spec.tax.mmBps,
    ethUsdPrice8: req.ethUsd8,
    // Open mode takes the factory's graduation trigger and has no deadline,
    // no per-wallet cap and no founder cut of the raise.
    targetRaiseWei: 0n,
    raiseDurationSecs: 0n,
    maxBuyWei: 0n,
    founderRaiseBps: 0,
    founderSupplyBps: 0,
    vestingSecs: 0,
    mode: spec.mode,
    minHoldForDividends: BigInt(spec.minHoldForDividends),
    dividendMode: spec.dividendMode,
    v3Path: "0x" as Hex,
  };

  const id = computeSpecId(spec);
  const value = creationFeeWei + firstBuyWei;
  const base: PublishResult = {
    dryRun: run.dryRun,
    specId: id,
    salt,
    firstBuyWei,
    metadataBytes: Buffer.byteLength(metadataURI, "utf8"),
  };

  if (run.dryRun) {
    // Simulate anyway: a dry run that reports success without the factory
    // agreeing is worth nothing. The operator must still be the caller.
    const operator = await publicClient.readContract({
      address: dep.contracts.launcher,
      abi: launcherAbi,
      functionName: "operator",
    });
    const { result } = await publicClient.simulateContract({
      address: dep.contracts.launcher,
      abi: launcherAbi,
      functionName: "launch",
      args: [dep.contracts.factory, params, salt, id, 0n],
      value,
      account: operator,
    });
    return { ...base, token: result[0], tokensOut: result[1] };
  }

  if (!run.privateKey) throw new PublishError("no operator key; set LUAF_PRIVATE_KEY or stay in dry run");
  const account = privateKeyToAccount(run.privateKey);
  const wallet = createWalletClient({ account, chain, transport: http(run.rpcUrl) }) as WalletClient;
  const { request, result } = await publicClient.simulateContract({
    address: dep.contracts.launcher,
    abi: launcherAbi,
    functionName: "launch",
    args: [dep.contracts.factory, params, salt, id, 0n],
    value,
    account,
  });
  const txHash = await wallet.writeContract(request);
  const receipt = await publicClient.waitForTransactionReceipt({ hash: txHash });
  if (receipt.status !== "success") throw new PublishError(`launch reverted: ${txHash}`);
  return { ...base, token: result[0], tokensOut: result[1], txHash };
}
