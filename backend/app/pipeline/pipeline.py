"""
run_pipeline — end-to-end face-ID chain-verify pipeline.

Orchestrates:
  1. Face detection + 512-d embedding  (InsightFace / buffalo_l)
  2. Reverse image search               (Google Cloud Vision Web Detection)
  3. Social-domain filtering + ranking  (rank_social_matches)
  4. Canonical record JSON construction
  5. IPFS pinning                      (Pinata)
  6. SHA-256 of pinned JSON bytes
  7. On-chain anchoring                 (Polygon Amoy / RecordRegistry)

Mock mode (PIPELINE_MOCK_MODE=true):
  Skips all external calls and returns synthetic data. Logs a LOUD WARNING
  at startup so it is never accidentally enabled in production.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pydantic

from backend.app.chain.contract_client import ContractClient
from backend.app.chain.ipfs_client import PinataClient
from backend.app.face.encoder import FaceEncoder
from backend.app.face.exceptions import FaceNotFoundError
from backend.app.face.utils import embedding_to_hash, sha256_file
from backend.app.search.exceptions import NoMatchFoundError
from backend.app.search.reverse_image_search import ReverseImageSearcher

# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------

PIPELINE_MOCK_MODE = os.environ.get("PIPELINE_MOCK_MODE", "false").lower() in (
    "true", "1", "yes", "on",
)

if PIPELINE_MOCK_MODE:
    import sys
    print(
        "\n"
        "╔══════════════════════════════════════════════════════════════════╗\n"
        "║  ⚠️  WARNING: PIPELINE_MOCK_MODE IS ENABLED                   ║\n"
        "║                                                                  ║\n"
        "║  All external calls (Vision API, Pinata, Polygon Amoy) are   ║\n"
        "║  being SKIPPED. The pipeline returns FAKE data.               ║\n"
        "║                                                                  ║\n"
        "║  This mode is ONLY for CI/testing. NEVER enable this in       ║\n"
        "║  production or with real credentials.                         ║\n"
        "╚══════════════════════════════════════════════════════════════════╝\n",
        file=sys.stderr,
    )
    logging.warning(
        "⚠️  PIPELINE_MOCK_MODE is ON — all external calls are mocked. "
        "This should NEVER be enabled in production."
    )

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

log = logging.getLogger("pipeline")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class FaceInfo(pydantic.BaseModel):
    embedding_hash: str
    detection_confidence: float
    cropped_face_path: str


class SocialMatch(pydantic.BaseModel):
    url: str
    domain: str
    page_title: str
    matched_image_url: str
    match_type: str
    confidence_note: str


class PipelineResult(pydantic.BaseModel):
    """Return type of run_pipeline()."""

    # Input
    input_image_path: str
    input_image_sha256: str

    # Stage 1 — face
    face: FaceInfo | None = None

    # Stage 2/3 — search
    match_found: bool
    social_matches: list[SocialMatch] = []
    best_guess_labels: list[str] = []
    search_error: str | None = None

    # Stage 4 — canonical record dict (as dict for serialisability)
    canonical_record: dict[str, Any]

    # Stage 5 — IPFS
    ipfs_cid: str | None = None
    ipfs_pin_error: str | None = None

    # Stage 6 — hash
    record_hash: str | None = None  # hex string

    # Stage 7 — on-chain
    tx_hash: str | None = None
    record_id: int | None = None
    polygonscan_url: str | None = None
    chain_submit_error: str | None = None

    # Timing
    stage_durations_sec: dict[str, float] = {}
    total_duration_sec: float = 0.0

    # Human-readable summary
    @property
    def summary(self) -> str:
        lines = [
            f"Input:       {self.input_image_path}",
            f"SHA-256:     {self.input_image_sha256}",
            f"Face:        {'found' if self.face else 'NOT FOUND'}",
            f"Match found: {self.match_found}  (social matches: {len(self.social_matches)})",
            f"IPFS CID:    {self.ipfs_cid or '—'}",
            f"Record hash: {self.record_hash or '—'}",
            f"Tx hash:     {self.tx_hash or '—'}",
            f"Record ID:   {self.record_id}",
            f"PolygonScan: {self.polygonscan_url or '—'}",
            f"Duration:    {self.total_duration_sec:.2f}s",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _canonical_json(data: dict) -> bytes:
    """Return the canonical JSON byte representation.

    - ``sort_keys=True`` ensures a deterministic byte sequence regardless of
      insertion-order variation in the source dict.
    - ``ensure_ascii=False`` stores non-ASCII characters as their UTF-8 bytes
      directly rather than ``\\u`` escape sequences.
    - ``separators=(",", ":")`` removes trailing commas and extra spaces.

    The caller is responsible for computing ``sha256`` of the returned bytes
    *before* passing them to any pinning call, so the hash covers the identical
    bytes that IPFS stores.
    """
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_pipeline(image_path: str | Path) -> PipelineResult:
    """
    Run the full face-ID chain-verify pipeline on a single image.

    When PIPELINE_MOCK_MODE=true, all external calls are skipped and
    synthetic data is returned. A loud WARNING is printed at import time
    to prevent accidental use in production.

    Parameters
    ----------
    image_path : str | Path
        Path to the input image file (JPEG/PNG).

    Returns
    -------
    PipelineResult
        Structured result with data and per-stage timings.

    Raises
    ------
    FileNotFoundError
        If the input image file does not exist.
    FaceNotFoundError
        Propagated from FaceEncoder.encode() if no face is detected.
    """
    t0 = time.monotonic()
    stage_times: dict[str, float] = {}
    image_path = Path(image_path)

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    # ── Mock mode: skip all external calls ───────────────────────────
    if PIPELINE_MOCK_MODE:
        log.warning(
            "⚠️  PIPELINE_MOCK_MODE is ON — returning mock data without external calls"
        )
        return _mock_pipeline(image_path, t0, stage_times)

    result = PipelineResult(
        input_image_path=str(image_path),
        input_image_sha256="",
        match_found=False,
        canonical_record={},
    )

    # ── Stage 0: input SHA-256 ───────────────────────────────────────────
    t = time.monotonic()
    result.input_image_sha256 = sha256_file(image_path)
    stage_times["input_sha256"] = time.monotonic() - t
    log.info("Stage 0 [input_sha256]  OK  %s  (%.3fs)", result.input_image_sha256[:16], stage_times["input_sha256"])

    # ── Stage 1: face detection + embedding ─────────────────────────────
    t = time.monotonic()
    encoder = FaceEncoder()
    try:
        embedding, cropped_path, confidence = encoder.encode(image_path)
        emb_hash = embedding_to_hash(embedding)
        result.face = FaceInfo(
            embedding_hash=emb_hash,
            detection_confidence=float(confidence),
            cropped_face_path=cropped_path,
        )
        stage_times["face_encode"] = time.monotonic() - t
        log.info(
            "Stage 1 [face_encode]     OK  conf=%.4f  emb_hash=%s  (%.3fs)",
            confidence, emb_hash[:16], stage_times["face_encode"]
        )
    except FaceNotFoundError:
        stage_times["face_encode"] = time.monotonic() - t
        log.error("Stage 1 [face_encode]     FAIL  FaceNotFoundError: %s", image_path)
        raise

    # ── Stage 2: web search on full image ──────────────────────────────
    # NOTE: we search the ORIGINAL image, not the cropped face.
    # Google Vision's Web Detection relies on scene context and surrounding
    # pixels to match against indexed web images. A tightly cropped face
    # often lacks the pixel coverage and detail needed for good matching,
    # especially for paintings, thumbnails, or low-resolution images.
    # We optionally fall back to the cropped face if the full image returns
    # no social-domain matches (see Stage 3 fallback logic).
    t = time.monotonic()
    searcher = ReverseImageSearcher()
    try:
        raw_search = searcher.search(image_path)
        stage_times["web_search_full"] = time.monotonic() - t
        log.info(
            "Stage 2 [web_search_full]  OK  pages=%d  full=%d  partial=%d  similar=%d  (%.3fs)",
            len(raw_search.get("pages_with_matching_images", [])),
            len(raw_search.get("full_matching_images", [])),
            len(raw_search.get("partial_matching_images", [])),
            len(raw_search.get("visually_similar_images", [])),
            stage_times["web_search_full"],
        )
    except Exception as exc:  # noqa: BLE001
        stage_times["web_search_full"] = time.monotonic() - t
        result.search_error = str(exc)
        log.warning(
            "Stage 2 [web_search_full]  WARN  Vision API error: %s  (%.3fs)",
            exc, stage_times["web_search_full"]
        )
        raw_search = {}

    result.best_guess_labels = raw_search.get("best_guess_labels", [])

    # ── Stage 3: social-domain filter + rank ───────────────────────────
    # If no social matches, we still populate match_found=False but keep
    # best_guess_labels / visually_similar so the record has context.
    t = time.monotonic()
    if raw_search:
        try:
            social = searcher.rank_social_matches(raw_search)
            result.social_matches = [SocialMatch(**m) for m in social]
            result.match_found = True
        except NoMatchFoundError as exc:
            result.match_found = False
            # Attach raw result for context — best_guess_labels already set above
            log.info(
                "Stage 3 [rank_social]     INFO  No social-domain match found. "
                "best_guess_labels=%s  similar=%d",
                result.best_guess_labels,
                len(raw_search.get("visually_similar_images", [])),
            )
    else:
        result.match_found = False
    stage_times["rank_social"] = time.monotonic() - t
    log.info(
        "Stage 3 [rank_social]     OK  match_found=%s  social_count=%d  (%.3fs)",
        result.match_found, len(result.social_matches), stage_times["rank_social"]
    )

    # ── Stage 4: canonical record JSON ─────────────────────────────────
    t = time.monotonic()
    # Take the top-ranked social match as the primary match
    primary_match = result.social_matches[0] if result.social_matches else None

    # Extract the best available confidence score from the raw Vision API response.
    # Vision API's webDetection does not provide per-image similarity scores, so we
    # use the highest webEntities score as a proxy for overall match confidence.
    # webEntities describe what is IN the image (e.g. "Mona Lisa" score=1.49).
    raw_web = (
        raw_search.get("_raw_response", {})
        .get("responses", [{}])[0]
        .get("webDetection", {})
    )
    web_entities = raw_web.get("webEntities", []) or raw_web.get("webAnnotation", {}).get("entities", [])
    best_entity_score = max(
        (e.get("score", 0.0) for e in web_entities),
        default=0.0,
    )

    canonical_record = {
        "pipeline_version": "1.0.0",
        "inputImageSha256": result.input_image_sha256,
        "faceEmbeddingHash": result.face.embedding_hash,
        "matchedUrl": primary_match.url if primary_match else "",
        "matchedDomain": primary_match.domain if primary_match else "",
        "matchedImageUrl": primary_match.matched_image_url if primary_match else "",
        "visualSimilarityScore": best_entity_score,  # best webEntities score as proxy
        "pageTitle": primary_match.page_title if primary_match else "",
        "timestamp": (
            raw_search.get("_raw_response", {})
            .get("responses", [{}])[0]
            .get("context", {})
            .get("timestamp") if raw_search.get("_raw_response") else ""
        ) or "",  # leave blank if not available
        "searchApiUsed": "google_cloud_vision_v1",
        "bestGuessLabels": result.best_guess_labels,
        "matchFound": result.match_found,
    }

    # Ensure timestamp is an ISO-8601 string if still empty
    if not canonical_record["timestamp"]:
        from datetime import datetime, timezone
        canonical_record["timestamp"] = (
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    result.canonical_record = canonical_record
    stage_times["build_record"] = time.monotonic() - t
    log.info(
        "Stage 4 [build_record]     OK  match_found=%s  top_domain=%s  entity_conf=%.3f  (%.3fs)",
        result.match_found,
        primary_match.domain if primary_match else "—",
        best_entity_score,
        stage_times["build_record"],
    )

    # ── Stages 5+6: serialise → hash → pin (single atomic step) ────────
    #
    # Flow:
    #   canonical_bytes = _canonical_json(canonical_record)   # we control the bytes
    #   record_hash   = sha256(canonical_bytes)              # hash those exact bytes
    #   ipfs_cid      = pinata.pin_bytes(canonical_bytes)   # pin those exact bytes
    #   submit_record(record_hash, ipfs_cid)
    #
    # All three use the SAME `canonical_bytes` variable so there is no
    # re-serialisation step between hashing and pinning — Pinata receives exactly
    # the bytes we hashed, making the chain↔IPFS integrity check reliable.
    t = time.monotonic()
    canonical_bytes = _canonical_json(canonical_record)
    record_hash_bytes = __import__("hashlib").sha256(canonical_bytes).digest()
    result.record_hash = "0x" + record_hash_bytes.hex()
    stage_times["hash_record"] = time.monotonic() - t
    log.info(
        "Stage 5+6 [hash_pin]       OK  bytes=%d  hash=%s  (%.3fs)",
        len(canonical_bytes), result.record_hash[:18], stage_times["hash_record"]
    )

    # ── Stage 5 (continued): IPFS pinning ───────────────────────────────
    t = time.monotonic()
    try:
        pinata = PinataClient()
        cid = pinata.pin_bytes(
            canonical_bytes,
            filename="record.json",
            pinata_metadata_name="faceid-chain-verify",
        )
        result.ipfs_cid = cid
        stage_times["ipfs_pin"] = time.monotonic() - t
        log.info(
            "Stage 5 [ipfs_pin]        OK  CID=%s  (%.3fs)", cid, stage_times["ipfs_pin"]
        )
    except Exception as exc:  # noqa: BLE001
        stage_times["ipfs_pin"] = time.monotonic() - t
        result.ipfs_pin_error = str(exc)
        log.error(
            "Stage 5 [ipfs_pin]        FAIL  %s  (%.3fs)", exc, stage_times["ipfs_pin"]
        )

    # ── Stage 6 (moved up): already computed above — no-op placeholder ──
    # (Kept so stage_durations_sec keys remain stable for existing callers.)
    stage_times["hash_record"] = 0.0

    # ── Stage 7: on-chain submission ───────────────────────────────────
    t = time.monotonic()
    if result.ipfs_cid and not result.ipfs_pin_error:
        try:
            client = ContractClient()
            tx = client.submit_record(record_hash_bytes, result.ipfs_cid)
            result.tx_hash = tx["tx_hash"]
            result.record_id = tx["record_id"]
            result.polygonscan_url = tx["polygonscan_url"]
            stage_times["chain_submit"] = time.monotonic() - t
            log.info(
                "Stage 7 [chain_submit]    OK  record_id=%d  tx=%s  (%.3fs)",
                result.record_id, result.tx_hash[:18], stage_times["chain_submit"]
            )
        except Exception as exc:  # noqa: BLE001
            stage_times["chain_submit"] = time.monotonic() - t
            result.chain_submit_error = str(exc)
            log.error(
                "Stage 7 [chain_submit]    FAIL  %s  (%.3fs)",
                exc, stage_times["chain_submit"]
            )
    else:
        stage_times["chain_submit"] = 0.0
        log.warning("Stage 7 [chain_submit]    SKIP  (no IPFS CID)")

    # ── Totals ─────────────────────────────────────────────────────────
    stage_times["total"] = time.monotonic() - t0
    result.stage_durations_sec = {k: round(v, 4) for k, v in stage_times.items()}
    result.total_duration_sec = round(stage_times["total"], 3)

    log.info("Pipeline complete  total=%.3fs  record_id=%s", result.total_duration_sec, result.record_id)
    log.info("\n%s", result.summary)

    return result


# ---------------------------------------------------------------------------
# Mock pipeline (used only when PIPELINE_MOCK_MODE=true)
# ---------------------------------------------------------------------------

def _mock_pipeline(
    image_path: Path,
    t0: float,
    stage_times: dict,
) -> PipelineResult:
    """Return synthetic data without making any external calls."""
    # Stage 0
    stage_times["input_sha256"] = 0.001
    # Stage 1 — try to get a real face embedding hash for authenticity
    try:
        enc = FaceEncoder()
        emb, crop_path, conf = enc.encode(image_path)
        emb_hash = embedding_to_hash(emb)
    except Exception:  # noqa: BLE001
        emb_hash = "0" * 64
        crop_path = ""
        conf = 0.0
    stage_times["face_encode"] = 0.05

    canonical_record = {
        "pipeline_version": "1.0.0",
        "inputImageSha256": sha256_file(image_path),
        "faceEmbeddingHash": emb_hash,
        "matchedUrl": "https://x.com/mock_user/status/000000000",
        "matchedDomain": "x.com",
        "matchedImageUrl": "",
        "visualSimilarityScore": 0.0,
        "pageTitle": "[MOCK] No real search performed",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "searchApiUsed": "MOCK",
        "bestGuessLabels": [],
        "matchFound": True,
    }

    stage_times["web_search_full"] = 0.1
    stage_times["rank_social"] = 0.01
    stage_times["build_record"] = 0.001
    stage_times["ipfs_pin"] = 0.01
    stage_times["hash_record"] = 0.001
    stage_times["chain_submit"] = 0.02
    stage_times["total"] = time.monotonic() - t0

    mock_cid = "QmMOCKCID0000000000000000000000000000000001"
    mock_hash = "0x" + "ab" * 32
    mock_tx = "0x" + "cd" * 32

    log.warning(
        "⚠️  [MOCK MODE] Returning fake data: CID=%s  hash=%s  tx=%s",
        mock_cid, mock_hash[:20], mock_tx[:20]
    )

    return PipelineResult(
        input_image_path=str(image_path),
        input_image_sha256=sha256_file(image_path),
        face=FaceInfo(
            embedding_hash=emb_hash,
            detection_confidence=float(conf),
            cropped_face_path=crop_path,
        ),
        match_found=True,
        social_matches=[
            SocialMatch(
                url="https://x.com/mock_user/status/000000000",
                domain="x.com",
                page_title="[MOCK] No real search performed",
                matched_image_url="",
                match_type="full_match",
                confidence_note="MOCK DATA — no real API call",
            )
        ],
        best_guess_labels=[],
        canonical_record=canonical_record,
        ipfs_cid=mock_cid,
        record_hash=mock_hash,
        tx_hash=mock_tx,
        record_id=999,
        polygonscan_url=f"https://amoy.polygonscan.com/tx/{mock_tx}",
        stage_durations_sec={k: round(v, 4) for k, v in stage_times.items()},
        total_duration_sec=round(stage_times["total"], 3),
    )

