import { HardhatUserConfig } from "hardhat/config";
import "@nomicfoundation/hardhat-toolbox";
import * as dotenv from "dotenv";

dotenv.config();

// Same compiler settings as DoublePlus: the vendored venture contracts must
// compile to the bytecode that is deployed. VentureFactory is optimised for
// size there (runs: 1), so it is here too.
const RPC_URL = process.env.RPC_URL ?? "";
const CHAIN_ID = Number(process.env.CHAIN_ID ?? 0);
const PRIVATE_KEY = process.env.PRIVATE_KEY;

// Forking, so a deploy can be rehearsed against the chain's real state before
// any of it is paid for. The scripts that need it documented these variables
// while the config had no forking block at all, which meant the rehearsal
// silently ran against an empty in-memory chain and then failed on a chain-id
// check for a reason that looked like a bug in the script.
const FORK_URL = process.env.FORK_URL ?? "";
const FORK_BLOCK = Number(process.env.FORK_BLOCK ?? 0);
// The forked network answers with the real chain's id, so a script's own
// "am I on the chain I think I am" check means something on a fork too.
const FORK_CHAIN_ID = Number(process.env.FORK_CHAIN_ID ?? 0);

const config: HardhatUserConfig = {
  solidity: {
    compilers: [{ version: "0.8.26", settings: { optimizer: { enabled: true, runs: 400 }, viaIR: true } }],
    overrides: {
      "contracts/vendor/doubleplus/v4/venture/VentureFactory.sol": {
        version: "0.8.26",
        settings: { optimizer: { enabled: true, runs: 1 }, viaIR: true },
      },
    },
  },
  networks: {
    hardhat: {
      allowUnlimitedContractSize: false,
      ...(FORK_URL
        ? {
            forking: { url: FORK_URL, ...(FORK_BLOCK ? { blockNumber: FORK_BLOCK } : {}) },
            ...(FORK_CHAIN_ID ? { chainId: FORK_CHAIN_ID } : {}),
          }
        : {}),
    },
    ...(RPC_URL
      ? { target: { url: RPC_URL, chainId: CHAIN_ID || undefined, accounts: PRIVATE_KEY ? [PRIVATE_KEY] : [] } }
      : {}),
  },
};

export default config;
