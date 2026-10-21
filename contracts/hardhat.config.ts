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
    hardhat: { allowUnlimitedContractSize: false },
    ...(RPC_URL
      ? { target: { url: RPC_URL, chainId: CHAIN_ID || undefined, accounts: PRIVATE_KEY ? [PRIVATE_KEY] : [] } }
      : {}),
  },
};

export default config;
