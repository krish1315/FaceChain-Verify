# faceid-chain-verify

**Detect a face in an image, search for it on the public web, pin the match record to IPFS, and anchor a tamper-evident proof on Polygon Amoy.**

The pipeline takes an image, extracts a 512-dimensional face embedding using InsightFace, searches the web via Google Cloud Vision's Web Detection API, filters results to social-media domains, builds a canonical JSON record, pins it to IPFS via Pinata, and writes a SHA-256 hash of that record onto a Polygon Amoy smart contract. The result is a publicly verifiable proof that a particular face image was linked to a particular URL at a particular moment — a tamper-evident anchor anyone can independently audit.

The focus is on **tamper-evidence**, not on-chain enforcement. The record hash stored on-chain is deterministic: anyone who has the same record JSON can recompute the hash and verify it matches what the contract stores. This makes the chain useful as a public, append-only notary for evidence, not as a system that controls value or permissions.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Architecture](#2-architecture)
3. [Tech stack](#3-tech-stack)
4. [Setup](#4-setup)
5. [How to run](#5-how-to-run)
6. [Blockchain: Polygon Amoy](#6-blockchain-why-polygon-amoy)
7. [Known limitations](#7-known-limitations)
8. [Responsible use](#8-responsible-use)
9. [License](#9-license)

---

## 1. What it does

`faceid-chain-verify` runs a 7-stage pipeline on a single image file. First it detects any faces and extracts a 512-dimensional embedding (InsightFace `buffalo_l`). It then sends the image to Google Cloud Vision's Web Detection API, which returns pages across the web that contain visually similar images — ranked by exact match, partial match, and visual similarity. Results are filtered to a fixed list of social-media domains (`x.com`, `twitter.com`, `instagram.com`, `facebook.com`, `linkedin.com`, `reddit.com`, `pinterest.com`, `tumblr.com`) so the pipeline stays focused on publicly posted social content. A canonical JSON record is built from the input image's SHA-256, the face embedding hash, and the top-ranked match metadata.

The record is then pinned to IPFS via Pinata, which returns a content-addressed CID. The canonical JSON bytes are hashed with SHA-256, and the resulting hash — along with the CID — is written in a single transaction to the `RecordRegistry` contract on Polygon Amoy. The transaction hash and assigned record ID form the public anchor.

Verification works in reverse: given a record ID, the contract returns the stored hash and CID; the verifier recomputes the SHA-256 of the record JSON and confirms it matches. No trust in the original submitter is required — the hash is deterministic and self-authenticating.

---

## 2. Architecture

```
┌──────────────┐     ┌────────────────────────────────────┐
│  Input Image │────▶│  Stage 1 — Face Detection         │
│  (JPEG/PNG)  │     │  + 512-d Embedding               │
│              │     │  InsightFace buffalo_l            │
└──────────────┘     └─────────────────┬──────────────────┘
                                       │
                                       ▼
                          ┌────────────────────────────────────┐
                          │  Stage 2 — Reverse Image Search   │
                          │  Google Cloud Vision               │
                          │  Web Detection API                │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Stage 3 — Domain Filter          │
                          │  Social media domains only        │
                          │  (x.com, instagram, reddit…)     │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Stage 4 — Canonical Record JSON  │
                          │  SHA-256, embedding hash, match   │
                          │  metadata, timestamps             │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Stage 5 — IPFS Pin              │
                          │  Pinata REST API → CID           │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Stage 6 — Record Hash            │
                          │  sha256(canonical_json_bytes)     │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Stage 7 — On-chain Anchor        │
                          │  Polygon Amoy / RecordRegistry    │
                          │  recordHash + CID + timestamp     │
                          └─────────────────┬──────────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────────┐
                          │  Verification (read-only)          │
                          │  Re-hash record JSON, compare    │
                          │  against on-chain value           │
                          └────────────────────────────────────┘
```

### Directory structure

```
faceid-chain-verify/
├── backend/
│   ├── app/
│   │   ├── face/           # InsightFace wrapper, embedding + hash
│   │   ├── search/         # Google Cloud Vision client, domain filter
│   │   ├── chain/          # Pinata IPFS client, Amoy contract client
│   │   ├── pipeline/       # Orchestration (run_pipeline, verify)
│   │   ├── utils/          # Retry with exponential backoff
│   │   └── api/            # FastAPI endpoints
│   └── tests/              # pytest suite (unit + integration)
├── contracts/
│   └── RecordRegistry.sol   # Smart contract source
├── frontend/                # Web UI (placeholder)
├── docs/                    # Architecture & design docs
├── .env.example             # Environment variable template
├── requirements.txt         # Pinned Python dependencies
└── requirements_lock.json   # Strict version lock for reproducibility
```

---

## 3. Tech stack

| Layer | Technology | Why this choice |
|---|---|---|
| **Face detection** | InsightFace (`buffalo_l`) | State-of-the-art open-source face analysis; 512-d embedding captures facial geometry; ONNX runtime means no GPU required for inference |
| **Reverse image search** | Google Cloud Vision Web Detection | REST-based, scalable, no infrastructure to manage; `pagesWithMatchingImages` gives page-level matches directly |
| **IPFS pinning** | Pinata | Developer-friendly REST API with SDK support; handles pinning infrastructure so the CID stays reachable |
| **Smart contract** | Polygon Amoy + Solidity | Amoy is an EVM-compatible PoS testnet; near-instant finality (~2s), near-zero gas fees; ideal for a demonstrator that anchors hashes |
| **Contract ABI / RPC** | `web3.py` + `eth-account` | Standard Python Ethereum stack; `eth-account` handles transaction signing without exposing private keys to RPC nodes |
| **Pipeline orchestration** | Python (plain) | No heavy framework needed; explicit stage tracking, typed Pydantic models, structured logging throughout |
| **API server** | FastAPI + Uvicorn | Async, auto-generates OpenAPI docs, native Pydantic validation for request/response schemas |
| **Input validation** | PIL (`Pillow`) | `Image.open().verify()` detects corrupt/non-image uploads before they reach expensive API calls |
| **Retry logic** | Custom decorator | Exponential backoff with jitter for transient HTTP/connection errors; explicit billing/auth errors fail fast without wasting retries |
| **Testing** | pytest | Standard Python test runner; module-level skip on missing credentials keeps the test suite usable in CI without real API keys |

---

## 4. Setup

### 4.1 Prerequisites

- Python 3.11+
- A Polygon Amoy wallet with MATIC (use the faucet below)
- Google Cloud account
- Pinata account

---

### 4.2 Google Cloud Vision API key

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create or select a project.
2. Enable the **Cloud Vision API** for the project.
3. Go to **APIs & Services → Credentials → Create Credentials → API Key**.
4. Copy the key — you will paste it into `.env` as `GOOGLE_VISION_API_KEY`.
5. **Important:** billing must be enabled on the project for the Vision API to work (Google's free tier covers 1,000 requests/month).

---

### 4.3 Pinata IPFS

1. Sign up at [pinata.cloud](https://pinata.cloud).
2. Go to **API Keys → New Key**.
3. Create a key with the `pinJSONToIPFS` scope (minimum required).
4. Copy both the **API Key** and **Secret API Key** — paste them into `.env`.

---

### 4.4 Polygon Amoy wallet + MATIC faucet

1. Install MetaMask or any EVM-compatible wallet.
2. Switch to the **Polygon Amoy** test network. RPC URL: `https://rpc-amoy.polygon.technology`. Chain ID: `80002`. Currency symbol: `MATIC`.
3. Get Amoy MATIC from the faucet: [faucet.polygon.technology](https://faucet.polygon.technology) — paste your Amoy wallet address, request test tokens (1 MATIC is more than enough for hundreds of records).
4. Export your wallet's **private key** (MetaMask: Account Details → Export Private Key). Paste it into `.env` as `DEPLOYER_PRIVATE_KEY` — **without the `0x` prefix**.

---

### 4.5 Deploy the smart contract

```bash
# 1. Navigate to contracts/
cd contracts

# 2. Install dependencies
npm install

# 3. Compile (requires Hardhat)
npx hardhat compile

# 4. Deploy to Amoy (edit hardhat.config.js first to set your wallet)
npx hardhat run scripts/deploy.js --network polygonAmoy
# → output: "Contract deployed to: 0x..."

# 5. Copy the deployed address and paste it into .env as CONTRACT_ADDRESS
```

The `RecordRegistry` contract source is in `contracts/contracts/RecordRegistry.sol`. It stores `{recordHash, ipfsCID, timestamp, submitter}` and exposes `submitRecord`, `getRecord`, `verifyRecord`, and `recordCount`.

---

### 4.6 Environment setup

```bash
# Copy the template
cp .env.example .env

# Open .env and fill in all values:
#   GOOGLE_VISION_API_KEY=AIza...
#   PINATA_API_KEY=...
#   PINATA_SECRET_API_KEY=...
#   POLYGON_AMOY_RPC_URL=https://rpc-amoy.polygon.technology
#   DEPLOYER_PRIVATE_KEY=<your-64-char-hex-key-without-0x>
#   CONTRACT_ADDRESS=0x...
```

---

### 4.7 Install Python dependencies

```bash
pip install -r requirements.txt
```

For strict reproducibility (e.g. in a CI/judge environment), verify against the lock file:

```bash
# Check installed versions match pinned versions
pip check
pytest backend/tests -v
```

---

## 5. How to run

### 5.1 Backend API

```bash
# Start the FastAPI server
uvicorn backend.app.api.main:app --reload --port 8000
```

The API is now available at `http://localhost:8000`. Open `http://localhost:8000/docs` for the interactive Swagger UI.

**Endpoints:**

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/pipeline/run` | Upload an image, returns a `job_id` immediately |
| `GET` | `/api/pipeline/status/{job_id}` | Poll for job status and result |
| `GET` | `/api/pipeline/verify/{record_id}` | Verify an on-chain record |
| `GET` | `/api/image-proxy?url=...` | Proxy an image URL (bypasses CORS/hotlink blocks) |

**Upload an image:**

```bash
curl -X POST http://localhost:8000/api/pipeline/run \
  -F "image=@my_photo.jpg"
# → {"job_id": "a1b2c3d4e5f6", "status": "pending", ...}
```

**Poll for result:**

```bash
curl http://localhost:8000/api/pipeline/status/a1b2c3d4e5f6
# → {"status": "done", "stage": "complete", "progress": 1.0,
#     "result": {"record_id": 1, "tx_hash": "0x...", ...}}
```

**Verify a record:**

```bash
curl "http://localhost:8000/api/pipeline/verify/1"
# → {"record_id": 1, "on_chain": true, "all_passed": true,
#     "checks": [{"name": "...", "passed": true, ...}]}
```

---

### 5.2 CLI

```bash
# Run the pipeline directly on a file
python -m backend.app.cli run backend/tests/fixtures/mona_lisa.jpg

# Verify an existing record
python -m backend.app.cli verify 1

# Run in mock mode (skips all external calls — for CI only)
PIPELINE_MOCK_MODE=true python -m backend.app.cli run test.jpg
```

---

### 5.3 Running tests

```bash
# Unit + integration tests (skips live API tests when credentials are missing)
pytest backend/tests -v

# Run only the API tests (fast, no external dependencies)
pytest backend/tests/test_api.py -v

# Run with mock mode for a full pipeline pass without real credentials
PIPELINE_MOCK_MODE=true pytest backend/tests/test_pipeline_e2e.py -v -s
```

> ⚠️ **Mock mode warning:** When `PIPELINE_MOCK_MODE=true`, a loud banner is printed to stderr. Never enable it in production or with real credentials — it returns fake data and never calls any external service.

---

## 6. Blockchain: why Polygon Amoy

**Polygon Amoy** is a Polygon Labs EVM-compatible proof-of-stake testnet. It was chosen for three reasons:

1. **Near-zero gas cost.** A `submitRecord` call costs roughly 0.0001–0.001 MATIC — well under $0.01 at current testnet token prices. This means hundreds or thousands of records can be anchored for the cost of a single dollar. There is no financial barrier to running the pipeline.

2. **Fast finality (~2 seconds).** The chain reaches finality in a few seconds, so a record is verifiable immediately after submission without waiting for multiple block confirmations.

3. **Full EVM compatibility.** The contract is written in Solidity and deployed to an EVM chain, so it works with any Ethereum tooling (`web3.py`, MetaMask, Etherscan, Hardhat). Switching to Polygon Mainnet, Sepolia, or any EVM L2 would require only changing the RPC URL and redeploying.

### ⚠️ This is a testnet, not mainnet

Amoy is a **test network** — it has no monetary value, and its records are **not permanent in the same sense as mainnet**.

What testnet means in practice:
- The chain and all its records can be reset or shut down at any time by Polygon Labs.
- There is no economic security: a testnet validator has no incentive to maintain the chain indefinitely.
- Records on testnet cannot be used as legal evidence in most jurisdictions — they exist purely to demonstrate the technical pipeline.

**For a production tamper-evidence system**, deploy the `RecordRegistry` contract to a persistent EVM mainnet (Polygon PoS, Ethereum Sepolia, etc.) where the chain has economic finality measured in years, not days.

The **tamper-evidence model** here is: *the on-chain hash is deterministic, so any holder of the canonical record JSON can independently verify it matches the stored anchor.* This works identically on testnet and mainnet — the difference is only how long the anchor persists.

---

## 7. Known limitations

**Be honest about what this system can and cannot do:**

- **Match quality depends on Google Vision's index.** Google Cloud Vision Web Detection is a general-purpose image search tool, not a face-search engine. It returns results based on what Google's crawler has indexed — meaning it only finds images that are already publicly accessible on the web. Images behind login walls, private accounts, or rare content that hasn't been crawled may not appear. Match quality also degrades for heavily cropped faces, unusual angles, low resolution, or heavy editing/artistic distortion.

- **Social domain filter is hardcoded.** The list of domains (`x.com`, `instagram.com`, etc.) is fixed in `backend/app/search/reverse_image_search.py`. There is no configuration file or environment variable to extend it. Adding domains requires a code change.

- **Single-face pipeline.** If an image contains multiple faces, the pipeline currently selects the largest/primary face. Multi-face handling is on the roadmap but not implemented.

- **Testnet records are impermanent.** As described in Section 6, records on Amoy have no guaranteed long-term persistence. A production deployment needs a mainnet chain.

- **Face recognition accuracy is image-dependent.** InsightFace's `buffalo_l` model performs well on well-lit, frontal images. It degrades on profile views, occluded faces, extreme expressions, or very low-resolution inputs. The `detection_confidence` score in the output lets callers decide whether to trust a result.

- **IPFS content persistence.** Pinata keeps content pinned as long as your account is active. If the Pinata account is deleted or the key is revoked, the CID may become unreachable from Pinata's gateway. Consider replicating pinning to a second provider for evidence-grade persistence.

- **No authentication on the API.** The FastAPI server currently has no auth — anyone who can reach the port can submit images and read records. A production deployment needs JWT/session auth and rate limiting.

- **No user consent flow.** The pipeline does not check or record whether the person in the submitted image consented to being searched. This is a significant ethical and legal gap — see Section 8.

---

## 8. Responsible use

**This project is a technical demonstrator.** It is designed to show how a face detection → web search → IPFS → blockchain pipeline can create tamper-evident evidence records. Any production use involves serious ethical and legal considerations.

**Privacy implications of face search:**

- Searching for a person's face on the web and linking it to their image is a significant privacy act. In many jurisdictions (GDPR EU, CCPA California, BIPA Illinois, etc.), processing biometric data including face embeddings may require explicit informed consent.
- Publishing records that link a person's face to their social media accounts without consent could expose individuals to harassment, doxxing, or doxxing-adjacent harms.
- The pipeline has no mechanism to check for consent, process opt-out requests, or handle takedown notices.

**Before any production use, at minimum:**
1. Implement a consent flow that records who submitted the image and that the subject consented (or that the submitter has the legal right to submit).
2. Add an opt-out / takedown mechanism to remove records associated with a specific identity.
3. Consult a privacy lawyer for the jurisdictions where you operate.
4. Consider whether face search is actually necessary for your use case — hash-only anchoring (without face embedding) may be sufficient for many tamper-evidence scenarios.

This project includes these warnings in the documentation rather than silently glossing over them, because glossing over them would be irresponsible.

---

## 9. License

MIT License

Copyright (c) 2026

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
