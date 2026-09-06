# Architecture

## Overview

`faceid-chain-verify` is a pipeline that detects a face in an input image,
generates a 512-d embedding, searches for visually similar images on the
web, pins the match record to IPFS, and anchors the record's hash on a
Polygon Amoy smart contract. The on-chain anchor makes the record
tamper-evident: anyone can verify that a given record was submitted by
checking the stored hash.

## Pipeline stages

```
┌──────────────┐     ┌─────────────────────┐
│  Input Image │────▶│  1. Face Detection  │
│  (JPEG/PNG)  │     │  + 512-d Embedding  │
└──────────────┘     │  (InsightFace,       │
                     │   buffalo_l)        │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  2. Reverse Image   │
                     │  Search             │
                     │  (Google Cloud      │
                     │   Vision Web        │
                     │   Detection)        │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  3. Domain Filter   │
                     │  Social media only  │
                     │  (x.com, twitter,   │
                     │   instagram, etc.)  │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  4. Canonical Record │
                     │  JSON               │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  5. Pin to IPFS      │
                     │  (Pinata) → CID      │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  6. Compute         │
                     │  sha256 of record   │
                     │  JSON               │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  7. Write to        │
                     │  Polygon Amoy        │
                     │  (recordHash,       │
                     │   ipfsCID,          │
                     │   timestamp,        │
                     │   submitter)        │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │  8. Verification    │
                     │  (re-hash + compare │
                     │   on-chain)         │
                     └─────────────────────┘
```

## Stage descriptions

### 1. Face Detection + Embedding

**Module:** `backend/app/face/`

- Accepts an image (JPEG or PNG).
- Uses **InsightFace** with the `buffalo_l` model to detect faces and
  extract a **512-dimensional embedding vector** per detected face.
- If multiple faces are detected, the pipeline currently selects the
  largest/primary face (future work: multi-face support).
- The embedding is stored as a NumPy array and also hashed (SHA-256 of
  the packed bytes) for inclusion in the canonical record.

### 2. Reverse Image Search

**Module:** `backend/app/search/`

- Uses the **Google Cloud Vision API** `webDetection` method.
- Sends the input image as base64-encoded data.
- Collects results from:
  - `pagesWithMatchingImages` — pages that contain visually near-identical
    images.
  - `partialMatchingImages` — pages with partially similar images.
- Each result includes: page URL, page title, matched image URL, and a
  (relative) visual similarity score.

### 3. Domain Filter

**Module:** `backend/app/search/`

- Filters the returned pages to **social media domains only**:
  - `x.com`, `twitter.com`
  - `instagram.com`
  - `facebook.com`
  - `linkedin.com`
  - `reddit.com`
  - `pinterest.com`
  - `tumblr.com`
- This makes the pipeline focused on publicly posted social content.
- Future work: allow domain allowlists to be configured.

### 4. Canonical Match Record JSON

**Module:** `backend/app/pipeline/`

The record is a JSON object with the following fields:

| Field | Type | Description |
|---|---|---|
| `inputImageSha256` | string | SHA-256 of the original input image bytes |
| `faceEmbeddingHash` | string | SHA-256 of the 512-d embedding bytes |
| `matchedUrl` | string | Full URL of the page containing the match |
| `matchedDomain` | string | Domain name extracted from matchedUrl |
| `matchedImageUrl` | string | Direct URL of the matched image |
| `visualSimilarityScore` | float | Score from Google Vision (0.0–1.0) |
| `pageTitle` | string | Title of the page (from Google Vision) |
| `timestamp` | string | ISO 8601 UTC timestamp of record creation |
| `searchApiUsed` | string | API identifier (e.g. `"google_cloud_vision_v1"`) |

Example:

```json
{
  "inputImageSha256": "a3f5c...",
  "faceEmbeddingHash": "b7e2d...",
  "matchedUrl": "https://x.com/user/status/123",
  "matchedDomain": "x.com",
  "matchedImageUrl": "https://pbs.twimg.com/media/abc.jpg",
  "visualSimilarityScore": 0.87,
  "pageTitle": "Tweet by @user",
  "timestamp": "2026-09-06T12:00:00Z",
  "searchApiUsed": "google_cloud_vision_v1"
}
```

### 5. IPFS Pinning (Pinata)

**Module:** `backend/app/chain/`

- The canonical record JSON is serialized to canonical JSON (sorted keys,
  no trailing commas).
- The JSON bytes are sent to **Pinata** via their REST API to pin.
- Pinata returns a **CID (Content Identifier)** for the pinned content.
- The CID provides a content-addressed reference to the record.

### 6. Record Hash Computation

**Module:** `backend/app/chain/`

- Computes `sha256(record_json_bytes)` — the JSON itself, not any field
  within it.
- This hash is deterministic: re-serializing the same record produces
  the same hash.
- The hash is stored on-chain so that anyone holding the record JSON can
  prove it matches what was anchored.

### 7. On-Chain Anchoring (Polygon Amoy)

**Module:** `backend/app/chain/`

- Connects to **Polygon Amoy testnet** via `web3.py`.
- Submits a transaction to a deployed smart contract calling
  `anchorRecord(recordHash, ipfsCID, timestamp, submitter)`.
- The transaction is signed with the deployer's private key from `.env`.
- The returned **transaction hash** is added to the response.

#### Contract interface (expected)

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract FaceIdAnchor {
    struct Record {
        bytes32 recordHash;
        string  ipfsCID;
        uint256 timestamp;
        address submitter;
        bool    exists;
    }

    mapping(bytes32 => Record) public records;

    event RecordAnchored(
        bytes32 indexed recordHash,
        string  ipfsCID,
        uint256 timestamp,
        address submitter
    );

    function anchorRecord(
        bytes32 recordHash,
        string calldata ipfsCID,
        uint256 timestamp,
        address submitter
    ) external {
        require(!records[recordHash].exists, "Already anchored");
        records[recordHash] = Record({
            recordHash: recordHash,
            ipfsCID:    ipfsCID,
            timestamp:  timestamp,
            submitter:  submitter,
            exists:     true
        });
        emit RecordAnchored(recordHash, ipfsCID, timestamp, submitter);
    }

    function verifyRecord(
        bytes32 recordHash,
        string calldata ipfsCID,
        uint256 timestamp,
        address submitter
    ) external view returns (bool) {
        Record memory r = records[recordHash];
        return r.exists
            && r.recordHash == recordHash
            && keccak256(abi.encodePacked(r.ipfsCID)) == keccak256(abi.encodePacked(ipfsCID))
            && r.timestamp == timestamp
            && r.submitter == submitter;
    }
}
```

### 8. Verification

**Module:** `backend/app/chain/`

- Given a record JSON, recompute `sha256(record_json_bytes)`.
- Query the smart contract: `records[computedHash]`.
- If `exists == true`, the record was anchored; if `exists == false`,
  it was not.
- The verification does **not** require trusting the submitter — the hash
  is deterministic, so the on-chain record is self-authenticating.

## Directory structure

```
backend/app/
├── face/        # InsightFace wrapper, embedding extraction
├── search/      # Google Cloud Vision client, domain filtering
├── chain/       # IPFS (Pinata) client, Polygon Amoy client, contract ABI
├── pipeline/    # Orchestration: runs stages 1–7
└── api/         # FastAPI endpoints (submit, verify, status)
```

## Data flow

```
Image bytes
    │
    ▼
[face detection] ──▶ face_embedding (512-d numpy array)
    │
    ▼
[compute faceEmbeddingHash = sha256(embedding.tobytes())]
    │
    ▼
[compute inputImageSha256 = sha256(image_bytes)]
    │
    ▼
[web search] ──▶ list of Match objects
    │
    ▼
[domain filter] ──▶ filtered Match objects
    │
    ▼
[build CanonicalRecord JSON]
    │
    ▼
[pin to IPFS] ──▶ ipfsCID
    │
    ▼
[compute recordHash = sha256(canonical_json_bytes)]
    │
    ▼
[submit to Polygon Amoy contract] ──▶ txHash
    │
    ▼
Return { record, ipfsCID, recordHash, txHash }
```

## Environment variables

| Variable | Description |
|---|---|
| `GOOGLE_VISION_API_KEY` | Google Cloud Vision API key |
| `PINATA_API_KEY` | Pinata API key |
| `PINATA_SECRET_API_KEY` | Pinata secret API key |
| `POLYGON_AMOY_RPC_URL` | Polygon Amoy RPC endpoint |
| `DEPLOYER_PRIVATE_KEY` | Private key for signing on-chain transactions |
| `CONTRACT_ADDRESS` | Deployed contract address on Amoy |

## Future work

- Multi-face support in a single image.
- Configurable domain allowlists for filtering.
- Additional reverse image search providers (TinEye, SerpAPI).
- NFT-style record ownership and transfer.
- Off-chain dispute resolution layer.
- Frontend for submitting images and verifying records.
