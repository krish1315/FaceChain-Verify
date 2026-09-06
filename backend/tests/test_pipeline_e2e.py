"""
End-to-end integration test for the full face-ID chain-verify pipeline.

This test makes REAL calls to:
  - Google Cloud Vision API  (web search)
  - Pinata IPFS             (pinning)
  - Polygon Amoy RPC        (contract submission + reads)

It is skipped at module level if any required environment variable is missing.
"""

import os
import sys
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Ensure backend/ is on the path
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    print(f"[WARN] No .env at {ENV_PATH} — reading from environment only.")

# --- Required env vars --------------------------------------------------------

REQUIRED = [
    "GOOGLE_VISION_API_KEY",
    "PINATA_API_KEY",
    "PINATA_SECRET_API_KEY",
    "POLYGON_AMOY_RPC_URL",
    "DEPLOYER_PRIVATE_KEY",
    "CONTRACT_ADDRESS",
]
missing = [v for v in REQUIRED if not os.environ.get(v, "").strip()]
if missing:
    pytest.skip(f"Missing env vars: {', '.join(missing)}", allow_module_level=True)


# --- Imports -----------------------------------------------------------------

from backend.app.pipeline.pipeline import run_pipeline


# --- Test image ---------------------------------------------------------------

# Use the mona_lisa fixture — a well-known face image likely indexed by Google.
TEST_IMAGE = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "mona_lisa.jpg"


# --- Tests --------------------------------------------------------------------

def test_e2e_pipeline_full_run():
    """
    Run the entire pipeline on the Mona Lisa fixture.

    This is the definitive integration test: face detection, Vision API search,
    IPFS pinning, and on-chain anchoring — all verified end-to-end.
    """
    assert TEST_IMAGE.exists(), (
        f"Test fixture not found: {TEST_IMAGE}. "
        "Run: python backend/tests/test_face.py  (fixture download step)"
    )

    print(f"\n{'='*70}")
    print(f"  E2E PIPELINE TEST — {TEST_IMAGE.name}")
    print(f"{'='*70}")

    t0 = time.time()
    result = run_pipeline(TEST_IMAGE)
    elapsed = time.time() - t0

    # -- Assertions ------------------------------------------------------
    assert result.input_image_sha256, "input SHA-256 must be set"
    assert result.face is not None, "face info must be populated"
    assert result.face.embedding_hash, "face embedding hash must be set"
    assert 0.0 < result.face.detection_confidence <= 1.0

    # IPFS
    assert result.ipfs_cid, (
        f"IPFS CID must be set. Error: {result.ipfs_pin_error}"
    )
    assert result.ipfs_cid.startswith(("Qm", "baf")), (
        f"Invalid CID format: {result.ipfs_cid}"
    )

    # Record hash
    assert result.record_hash, "record hash must be set"
    assert result.record_hash.startswith("0x"), "record hash must be 0x-prefixed"
    assert len(result.record_hash) == 66, "record hash must be 66 chars (0x + 64 hex)"

    # On-chain
    assert result.tx_hash, (
        f"Transaction hash must be set. Error: {result.chain_submit_error}"
    )
    assert result.tx_hash.startswith("0x"), "tx hash must be 0x-prefixed"
    assert result.record_id is not None and result.record_id >= 1
    assert result.polygonscan_url, "PolygonScan URL must be set"
    assert "amoy.polygonscan.com/tx/" in result.polygonscan_url

    # Duration sanity check — whole pipeline should complete in <5 min
    assert result.total_duration_sec < 300, (
        f"Pipeline took {result.total_duration_sec:.1f}s (>5min threshold)"
    )

    # -- Stage timing breakdown ------------------------------------------
    print(f"\n{'-'*70}")
    print(f"  STAGE TIMINGS")
    print(f"{'-'*70}")
    for stage, dur in result.stage_durations_sec.items():
        pct = dur / result.total_duration_sec * 100 if result.total_duration_sec else 0
        print(f"  {stage:<25}  {dur:>7.3f}s  ({pct:>5.1f}%)")

    # -- Final clickable URLs ---------------------------------------------
    print(f"\n{'-'*70}")
    print(f"  VERIFICATION LINKS")
    print(f"{'-'*70}")
    print(f"  IPFS Gateway:  https://gateway.pinata.cloud/ipfs/{result.ipfs_cid}")
    print(f"  PolygonScan:   {result.polygonscan_url}")
    print(f"  Record ID:     {result.record_id}")
    print(f"  Record hash:   {result.record_hash}")
    print(f"  Total time:    {elapsed:.2f}s")

    # -- Human-readable summary ------------------------------------------
    print(f"\n{'-'*70}")
    print(f"  PIPELINE RESULT SUMMARY")
    print(f"{'-'*70}")
    print(result.summary)
    print(f"{'='*70}")


def test_pipeline_with_no_face_image_propagates_error():
    """
    Confirm that a landscape image (no face) raises FaceNotFoundError
    rather than silently proceeding.
    """
    landscape = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "landscape.jpg"
    if not landscape.exists():
        pytest.skip("landscape.jpg fixture not found")

    from backend.app.face.exceptions import FaceNotFoundError

    with pytest.raises(FaceNotFoundError):
        run_pipeline(landscape)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
