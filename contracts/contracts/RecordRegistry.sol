// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title RecordRegistry
 * @notice Minimal on-chain registry for anchoring face-match records on Polygon Amoy.
 * @dev Stores recordHash + ipfsCID for each submission, with a simple
 *      verifyRecord helper that compares a stored hash to an expected hash.
 */
contract RecordRegistry {
    struct Record {
        bytes32 recordHash;
        string  ipfsCID;
        uint256 timestamp;
        address submitter;
    }

    // recordId (1-indexed) -> Record
    mapping(uint256 => Record) public records;

    // Auto-incremented record id; starts at 0, first submit returns 1
    uint256 public recordCount;

    event RecordSubmitted(
        uint256 indexed recordId,
        bytes32 indexed recordHash,
        string  ipfsCID,
        address indexed submitter,
        uint256 timestamp
    );

    /**
     * @notice Submit a new record.
     * @param recordHash  SHA-256 of the canonical match record JSON.
     * @param ipfsCID     IPFS CID of the pinned record JSON.
     * @return recordId   The newly-assigned id (1-indexed).
     */
    function submitRecord(
        bytes32 recordHash,
        string calldata ipfsCID
    ) external returns (uint256 recordId) {
        recordCount += 1;
        recordId = recordCount;

        records[recordId] = Record({
            recordHash: recordHash,
            ipfsCID:    ipfsCID,
            timestamp:  block.timestamp,
            submitter:  msg.sender
        });

        emit RecordSubmitted(
            recordId,
            recordHash,
            ipfsCID,
            msg.sender,
            block.timestamp
        );
    }

    /**
     * @notice Fetch a record by id.
     * @param recordId  The id returned from submitRecord.
     * @return recordHash   The stored record hash.
     * @return ipfsCID      The stored IPFS CID.
     * @return timestamp    The block timestamp at submission.
     * @return submitter    The address that submitted the record.
     */
    function getRecord(uint256 recordId)
        external
        view
        returns (
            bytes32 recordHash,
            string  memory ipfsCID,
            uint256 timestamp,
            address submitter
        )
    {
        Record storage r = records[recordId];
        return (r.recordHash, r.ipfsCID, r.timestamp, r.submitter);
    }

    /**
     * @notice Verify a record id by comparing its stored hash to an expected hash.
     * @param recordId      The id returned from submitRecord.
     * @param expectedHash  The hash the caller recomputed locally.
     * @return True if a record exists at `recordId` AND its hash matches.
     */
    function verifyRecord(
        uint256 recordId,
        bytes32 expectedHash
    ) external view returns (bool) {
        if (recordId == 0 || recordId > recordCount) {
            return false;
        }
        Record storage r = records[recordId];
        return r.recordHash == expectedHash;
    }
}
