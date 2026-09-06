"""Live integration tests for the chain module.

These tests make REAL calls to:
  1. Pinata IPFS API  — pins JSON, returns CID
  2. Polygon Amoy RPC  — submits transactions, reads state

They are skipped if any required environment variable is missing.
Requires a deployed RecordRegistry contract at CONTRACT_ADDRESS on Amoy.
"""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Ensure backend/ is on the path
BACKEND_DIR = Path(__file__).parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Load .env from the project root
PROJECT_ROOT = BACKEND_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    print(f"[WARN] No .env file at {ENV_PATH} — reading from environment only.")

# ─── Env check ────────────────────────────────────────────────────────────────

REQUIRED_VARS = [
    "PINATA_API_KEY",
    "PINATA_SECRET_API_KEY",
    "POLYGON_AMOY_RPC_URL",
    "DEPLOYER_PRIVATE_KEY",
    "CONTRACT_ADDRESS",
]

_missing = [v for v in REQUIRED_VARS if not os.environ.get(v, "").strip()]
if _missing:
    pytest.skip(f"Missing env vars: {', '.join(_missing)}", allow_module_level=True)


# ─── Module imports ───────────────────────────────────────────────────────────

from backend.app.chain.contract_client import ContractClient
from backend.app.chain.ipfs_client import PinataClient, PinataAPIError

# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pinata() -> PinataClient:
    return PinataClient()


@pytest.fixture(scope="module")
def contract() -> ContractClient:
    return ContractClient()


@pytest.fixture(scope="module")
def sample_record() -> dict:
    """Minimal canonical record used for all tests."""
    return {
        "inputImageSha256": "a" * 64,
        "faceEmbeddingHash": "b" * 64,
        "matchedUrl": "https://x.com/example/status/123",
        "matchedDomain": "x.com",
        "matchedImageUrl": "https://pbs.twimg.com/media/test.jpg",
        "visualSimilarityScore": 0.85,
        "pageTitle": "Example Tweet",
        "timestamp": "2026-09-06T12:00:00Z",
        "searchApiUsed": "google_cloud_vision_v1",
    }


# ─── IPFS tests ───────────────────────────────────────────────────────────────

def test_pin_json_returns_valid_cid(pinata, sample_record):
    """Pin a small sample JSON to IPFS and assert we get a valid CID back."""
    cid = pinata.pin_json(sample_record, pinata_metadata_name="test-sample-record")

    # CIDv1 is a base32-encoded string starting with "Qm" or "bafy"
    assert isinstance(cid, str), f"CID should be a string, got {type(cid)}"
    assert len(cid) > 20, f"CID too short: {cid!r}"
    # Either CIDv0 (Qm...) or CIDv1 (bafy...)
    assert cid.startswith("Qm") or cid.startswith("baf"), (
        f"Invalid CID format: {cid!r}"
    )

    print(f"\n[test_pin_json_returns_valid_cid]")
    print(f"  Sample record pinned to IPFS")
    print(f"  IPFS CID: {cid}")
    print(f"  IPFS Gateway: https://gateway.pinata.cloud/ipfs/{cid}")


# ─── Contract tests ────────────────────────────────────────────────────────────

def test_submit_record_and_get_record(contract, sample_record):
    """Submit a record hash, confirm it's stored, print tx hash and PolygonScan link."""
    # Compute the SHA-256 of the canonical JSON bytes
    json_bytes = json.dumps(sample_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_hash = hashlib.sha256(json_bytes).digest()

    result = contract.submit_record(record_hash, "QmTestCID123456789abcdefghij")

    assert "tx_hash" in result
    assert "record_id" in result
    assert "polygonscan_url" in result
    assert result["tx_hash"].startswith("0x")
    assert "amoy.polygonscan.com/tx/" in result["polygonscan_url"]

    print(f"\n[test_submit_record_and_get_record]")
    print(f"  record_id:  {result['record_id']}")
    print(f"  tx_hash:    {result['tx_hash']}")
    print(f"  PolygonScan: {result['polygonscan_url']}")

    # ── Read back ───────────────────────────────────────────────────────────
    on_chain = contract.get_record(result["record_id"])

    assert on_chain["recordHash"] == "0x" + record_hash.hex()
    assert on_chain["ipfsCID"] == "QmTestCID123456789abcdefghij"
    assert on_chain["submitter"] == contract.account.address

    print(f"  Stored hash:  {on_chain['recordHash']}")
    print(f"  Stored CID:   {on_chain['ipfsCID']}")
    print(f"  Submitter:    {on_chain['submitter']}")


def test_verify_record_happy_path(contract, sample_record):
    """verify_record returns True when the correct hash is passed."""
    json_bytes = json.dumps(sample_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_hash = hashlib.sha256(json_bytes).digest()

    result = contract.submit_record(record_hash, "QmVerifyHappyCID1234567890ABC")
    record_id = result["record_id"]

    is_valid = contract.verify_record(record_id, record_hash)

    assert is_valid is True, "verify_record should return True for the correct hash"

    print(f"\n[test_verify_record_happy_path]")
    print(f"  record_id: {record_id}")
    print(f"  verify_record(record_id, correct_hash) = {is_valid}")


def test_verify_record_tampered_hash_returns_false(contract, sample_record):
    """verify_record returns False when a tampered/wrong hash is passed."""
    json_bytes = json.dumps(sample_record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    record_hash = hashlib.sha256(json_bytes).digest()

    result = contract.submit_record(record_hash, "QmTamperCID1234567890ABCD")
    record_id = result["record_id"]

    # Deliberately wrong hash (different JSON)
    tampered_record = {**sample_record, "inputImageSha256": "z" * 64}
    tampered_bytes = json.dumps(
        tampered_record, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    tampered_hash = hashlib.sha256(tampered_bytes).digest()

    is_valid = contract.verify_record(record_id, tampered_hash)

    assert is_valid is False, (
        "verify_record should return False for a tampered/wrong hash"
    )

    print(f"\n[test_verify_record_tampered_hash_returns_false]")
    print(f"  record_id: {record_id}")
    print(f"  verify_record(record_id, tampered_hash) = {is_valid}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
