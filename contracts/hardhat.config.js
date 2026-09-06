require("dotenv").config({ path: require("path").resolve(__dirname, "..", ".env") });

require("@nomicfoundation/hardhat-toolbox");

const POLYGON_AMOY_RPC_URL =
  process.env.POLYGON_AMOY_RPC_URL || "https://rpc-amoy.polygon.technology";
const DEPLOYER_PRIVATE_KEY = process.env.DEPLOYER_PRIVATE_KEY || "";

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: {
    version: "0.8.20",
    settings: {
      optimizer: {
        enabled: true,
        runs: 200,
      },
    },
  },
  networks: {
    // In-process Hardhat Network for tests; no external network required.
    hardhat: {
      chainId: 31337,
    },
    // Polygon Amoy testnet. Requires DEPLOYER_PRIVATE_KEY to be set.
    amoy: {
      url: POLYGON_AMOY_RPC_URL,
      chainId: 80002,
      accounts: DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [],
    },
  },
};
