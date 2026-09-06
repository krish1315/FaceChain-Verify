/**
 * Deploy RecordRegistry to Polygon Amoy testnet.
 *
 * Usage:
 *   npx hardhat run scripts/deploy.js --network amoy
 *
 * Environment (loaded from .env at project root):
 *   POLYGON_AMOY_RPC_URL   — RPC endpoint for Polygon Amoy
 *   DEPLOYER_PRIVATE_KEY   — Private key of the deployer account
 */

const hre = require("hardhat");

async function main() {
  console.log("Deploying RecordRegistry to", hre.network.name, "...");

  const RecordRegistry = await hre.ethers.getContractFactory("RecordRegistry");
  const contract = await RecordRegistry.deploy();

  await contract.waitForDeployment();
  const address = await contract.getAddress();

  console.log(`RecordRegistry deployed to: ${address}`);
  console.log(`\nPaste this address into .env as CONTRACT_ADDRESS=${address}`);
}

main()
  .then(() => process.exit(0))
  .catch((err) => {
    console.error(err);
    process.exit(1);
  });
