"""
FastAPI tests using TestClient.

These tests cover only the job-tracking logic of /api/pipeline/run and
/api/pipeline/status, NOT the full pipeline (which is covered by the
end-to-end test in test_pipeline_e2e.py). This keeps the API tests fast
and not dependent on external services.
"""

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Disable logging during tests to keep output clean
import logging
logging.disable(logging.CRITICAL)


# Import AFTER path setup
from fastapi.testclient import TestClient  # noqa: E402

# Stub the run_pipeline call so we don't hit Vision API/Pinata/chain.
# We import main only after stubbing so its module-level imports succeed.
import backend.app.api.main as main_module  # noqa: E402

client = TestClient(main_module.app)


# ─── Fixtures ────────────────────────────────────────────────────────────────

TEST_IMAGE = PROJECT_ROOT / "backend" / "tests" / "fixtures" / "mona_lisa.jpg"


# ─── Tests ───────────────────────────────────────────────────────────────────

def test_root_endpoint():
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["service"] == "faceid-chain-verify"
    assert "endpoints" in data


def test_health_endpoint():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_run_rejects_non_image_upload():
    """A non-image content type should be rejected with 400."""
    r = client.post(
        "/api/pipeline/run",
        files={"image": ("foo.txt", b"not an image", "text/plain")},
    )
    assert r.status_code == 400
    assert "image" in r.json()["detail"].lower()


def test_status_returns_404_for_unknown_job():
    r = client.get("/api/pipeline/status/does-not-exist")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_job_created_pending_then_done(monkeypatch):
    """
    Submit a job, then poll its status until 'done'. We monkey-patch
    run_pipeline to a fast stub so this test doesn't depend on external
    services.
    """
    if not TEST_IMAGE.exists():
        pytest.skip(f"Test image not found: {TEST_IMAGE}")

    # ── Stub: replace run_pipeline with a fake that fills a result dict ───
    import threading

    def fake_run_pipeline(image_path):
        from backend.app.pipeline.pipeline import PipelineResult
        from backend.app.pipeline.pipeline import FaceInfo

        return PipelineResult(
            input_image_path=str(image_path),
            input_image_sha256="0" * 64,
            face=FaceInfo(
                embedding_hash="1" * 64,
                detection_confidence=0.95,
                cropped_face_path="/tmp/fake.jpg",
            ),
            match_found=True,
            social_matches=[],
            best_guess_labels=["Stub Label"],
            ipfs_cid="QmStubCID1234567890ABCDEFGHIJKL",
            record_hash="0x" + "ab" * 32,
            tx_hash="0x" + "cd" * 32,
            record_id=42,
            polygonscan_url="https://amoy.polygonscan.com/tx/0xcdcdcdcd",
            stage_durations_sec={"total": 0.1},
            total_duration_sec=0.1,
            canonical_record={"stub": True},
        )

    monkeypatch.setattr(main_module, "run_pipeline", fake_run_pipeline)

    # ── Submit ──────────────────────────────────────────────────────────
    with open(TEST_IMAGE, "rb") as f:
        r = client.post(
            "/api/pipeline/run",
            files={"image": ("mona.jpg", f, "image/jpeg")},
        )
    assert r.status_code == 200
    submit_data = r.json()
    assert "job_id" in submit_data
    job_id = submit_data["job_id"]
    assert submit_data["status"] in ("pending", "running", "done")

    # ── Poll until done (with timeout) ───────────────────────────────────
    deadline = time.time() + 15
    final = None
    while time.time() < deadline:
        s = client.get(f"/api/pipeline/status/{job_id}")
        assert s.status_code == 200
        sd = s.json()
        if sd["status"] in ("done", "error"):
            final = sd
            break
        time.sleep(0.1)

    assert final is not None, f"Job {job_id} never finished"
    assert final["status"] == "done", f"Expected 'done', got {final['status']!r}; error={final.get('error')}"

    # ── Verify the final result payload ──────────────────────────────────
    assert final["progress"] == 1.0
    assert final["stage"] == "complete"
    assert final["result"] is not None
    assert final["result"]["ipfs_cid"] == "QmStubCID1234567890ABCDEFGHIJKL"
    assert final["result"]["record_id"] == 42
    assert final["result"]["polygonscan_url"].startswith("https://amoy.polygonscan.com/tx/")


def test_cors_headers_on_root():
    """CORS middleware should add the allow-origin header for localhost dev."""
    r = client.get("/", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    # CORSMiddleware sets access-control-allow-origin
    assert r.headers.get("access-control-allow-origin") is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
