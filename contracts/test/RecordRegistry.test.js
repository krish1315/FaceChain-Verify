const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("RecordRegistry", function () {
  let RecordRegistry;
  let contract;
  let deployer;
  let other;

  // Sample values
  const sampleHash = ethers.keccak256(ethers.toUtf8Bytes("sample-record-json"));
  const sampleCID  = "QmTzQ1NjAwS2VfHjEsM1xEmL1VGJrD3bH9YQNSqJfYvJ9D";

  beforeEach(async function () {
    [deployer, other] = await ethers.getSigners();
    RecordRegistry = await ethers.getContractFactory("RecordRegistry");
    contract = await RecordRegistry.deploy();
    await contract.waitForDeployment();
  });

  describe("submitRecord", function () {
    it("should store a record and emit RecordSubmitted", async function () {
      await expect(contract.submitRecord(sampleHash, sampleCID))
        .to.emit(contract, "RecordSubmitted")
        .withArgs(
          1,                          // recordId
          sampleHash,                 // recordHash
          sampleCID,                  // ipfsCID
          deployer.address,           // submitter
          (ts) => typeof ts === "bigint" // timestamp (any value, just check type)
        );

      expect(await contract.recordCount()).to.equal(1);
    });

    it("should auto-increment recordId for multiple submissions", async function () {
      const hash2 = ethers.keccak256(ethers.toUtf8Bytes("record-2"));
      const cid2  = "QmSecondCid1234567890abcdefghijklmnopqrstuvwxyzABCD";

      await contract.submitRecord(sampleHash, sampleCID);
      await expect(contract.submitRecord(hash2, cid2))
        .to.emit(contract, "RecordSubmitted")
        .withArgs(2, hash2, cid2, deployer.address, (ts) => typeof ts === "bigint");

      expect(await contract.recordCount()).to.equal(2);
    });

    it("should record the msg.sender as submitter", async function () {
      const tx = await contract.connect(other).submitRecord(sampleHash, sampleCID);
      await tx.wait();

      const [, , , submitter] = await contract.getRecord(1);
      expect(submitter).to.equal(other.address);
    });
  });

  describe("getRecord", function () {
    it("should return the stored record fields", async function () {
      const tx = await contract.submitRecord(sampleHash, sampleCID);
      const receipt = await tx.wait();
      const block = await ethers.provider.getBlock(receipt.blockNumber);
      const expectedTs = block.timestamp;

      const [hash, cid, ts, submitter] = await contract.getRecord(1);

      expect(hash).to.equal(sampleHash);
      expect(cid).to.equal(sampleCID);
      expect(ts).to.equal(expectedTs);
      expect(submitter).to.equal(deployer.address);
    });
  });

  describe("verifyRecord", function () {
    it("should return true when the expected hash matches the stored hash", async function () {
      await contract.submitRecord(sampleHash, sampleCID);
      expect(await contract.verifyRecord(1, sampleHash)).to.equal(true);
    });

    it("should return false when the expected hash is tampered", async function () {
      await contract.submitRecord(sampleHash, sampleCID);
      const tamperedHash = ethers.keccak256(ethers.toUtf8Bytes("tampered"));
      expect(await contract.verifyRecord(1, tamperedHash)).to.equal(false);
    });

    it("should return false for non-existent record ids", async function () {
      expect(await contract.verifyRecord(999, sampleHash)).to.equal(false);
      expect(await contract.verifyRecord(0, sampleHash)).to.equal(false);
    });
  });
});
