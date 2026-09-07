"""
FastAPI app for the face-ID chain-verify pipeline.

Endpoints:
  POST /api/pipeline/run         — multipart image upload, runs pipeline in background
  GET  /api/pipeline/status/{id} — current stage/progress + final result when done
  GET  /api/pipeline/verify/{id} — runs verify_record and returns 3-check report
  GET  /api/image-proxy          — proxies an image URL to bypass browser CORS/hotlink
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend.app.pipeline.pipeline import run_pipeline
from backend.app.pipeline.verify import verify_record

# Load environment variables from .env at startup
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("api")

# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

app = FastAPI(
    title="faceid-chain-verify API",
    version="1.0.0",
    description="End-to-end face detection, reverse image search, IPFS, and on-chain anchoring.",
)

# Allow any localhost origin for frontend dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:8000",
        "*",  # permissive for dev; tighten for prod
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Job store (thread-safe in-memory)
# ---------------------------------------------------------------------------

class JobState:
    """Mutable job state shared between the request handler and background task."""

    def __init__(self, job_id: str, image_path: str):
        self.job_id = job_id
        self.image_path = image_path
        self.status = "pending"            # pending | running | done | error
        self.stage = "queued"              # human-readable current stage
        self.progress = 0.0                # 0.0 -> 1.0
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.completed_at: str | None = None
        self.error: str | None = None
        self.result: dict | None = None
        self._lock = threading.Lock()

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "stage": self.stage,
                "progress": self.progress,
                "created_at": self.created_at,
                "completed_at": self.completed_at,
                "error": self.error,
                "result": self.result,
            }


_jobs: dict[str, JobState] = {}
_jobs_lock = threading.Lock()

# Uploaded-image temp directory
UPLOAD_DIR = Path(tempfile.gettempdir()) / "faceid_uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _get_job(job_id: str) -> JobState:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------

_STAGE_PROGRESS = {
    "input_sha256":  0.10,
    "face_encode":   0.20,
    "web_search_full": 0.45,
    "rank_social":   0.55,
    "build_record":  0.60,
    "ipfs_pin":      0.75,
    "hash_record":   0.80,
    "chain_submit":  0.95,
    "total":         1.00,
}


def _run_pipeline_job(job_id: str, image_path: str) -> None:
    """Run the pipeline in a thread, updating job state at each stage."""
    job = _get_job(job_id)
    try:
        with job._lock:
            job.status = "running"
            job.stage = "starting"
            job.progress = 0.0

        # Run the pipeline — it returns a PipelineResult.
        # We don't get mid-stage callbacks from run_pipeline directly, so
        # we monkey-patch the logger to capture stage progress.
        stage_progress = dict(_STAGE_PROGRESS)

        # Hook into the pipeline logger
        pipeline_log = logging.getLogger("pipeline")
        old_level = pipeline_log.level
        pipeline_log.setLevel(logging.INFO)

        original_emit = pipeline_log.handlers[:]

        class _ProgressHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                msg = record.getMessage()
                # Heuristic: messages look like "Stage N [stage_name]  ..."
                if "Stage" in msg and "[" in msg:
                    try:
                        inside = msg.split("[", 1)[1].split("]")[0]
                        stage = inside.strip().lower()
                        if stage in stage_progress:
                            with job._lock:
                                job.stage = stage
                                job.progress = stage_progress[stage]
                    except Exception:
                        pass

        progress_handler = _ProgressHandler(level=logging.INFO)
        pipeline_log.addHandler(progress_handler)

        try:
            result = run_pipeline(image_path)
        finally:
            pipeline_log.removeHandler(progress_handler)
            pipeline_log.setLevel(old_level)

        # Detect whether the chain write succeeded or failed.
        # A non-null chain_submit_error means the pipeline reached IPFS but
        # failed on the on-chain step. In that case we surface the error as a
        # job-level failure so the frontend never shows "done" with empty
        # tx_hash / record_id without an explanation.
        chain_failed = result.chain_submit_error is not None

        if chain_failed:
            log.error(
                "Pipeline job %s completed with chain_write failure: %s",
                job_id, result.chain_submit_error
            )
            with job._lock:
                job.status = "error"
                job.stage = "chain_write_failed"
                job.error = (
                    f"Chain write failed: {result.chain_submit_error}. "
                    "The record was pinned to IPFS (see ipfs_cid below) but "
                    "could not be anchored on-chain. "
                    "This is likely a wallet / RPC / contract configuration issue."
                )
                job.progress = 1.0
                job.completed_at = datetime.now(timezone.utc).isoformat()
            # Still pack the full result so the frontend can show the IPFS CID.
        else:
            with job._lock:
                job.status = "done"
                job.stage = "complete"
                job.progress = 1.0
                job.completed_at = datetime.now(timezone.utc).isoformat()

        # Convert PipelineResult to a dict for JSON storage
        result_dict = {
            "input_image_path": result.input_image_path,
            "input_image_sha256": result.input_image_sha256,
            "face": result.face.model_dump() if result.face else None,
            "match_found": result.match_found,
            "social_matches": [m.model_dump() for m in result.social_matches],
            "best_guess_labels": result.best_guess_labels,
            "search_error": result.search_error,
            "canonical_record": result.canonical_record,
            "ipfs_cid": result.ipfs_cid,
            "ipfs_pin_error": result.ipfs_pin_error,
            "record_hash": result.record_hash,
            "tx_hash": result.tx_hash,
            "record_id": result.record_id,
            "polygonscan_url": result.polygonscan_url,
            "chain_submit_error": result.chain_submit_error,
            "chain_write_status": "failed" if chain_failed else "ok",
            "stage_durations_sec": result.stage_durations_sec,
            "total_duration_sec": result.total_duration_sec,
        }
        job.result = result_dict

    except Exception as exc:  # noqa: BLE001
        log.exception("Pipeline job %s failed", job_id)
        with job._lock:
            job.status = "error"
            job.stage = "failed"
            job.error = str(exc)
            job.completed_at = datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def root() -> dict:
    return {
        "service": "faceid-chain-verify",
        "version": app.version,
        "docs": "/docs",
        "endpoints": [
            "POST /api/pipeline/run",
            "GET  /api/pipeline/status/{job_id}",
            "GET  /api/pipeline/result/{job_id}/face-crop",
            "GET  /api/pipeline/verify/{record_id}",
            "GET  /api/pipeline/proxy-image?url=...",
        ],
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/pipeline/run")
async def pipeline_run(
    background_tasks: BackgroundTasks,
    image: UploadFile = File(...),
) -> dict:
    """
    Accept a multipart image upload. Saves the upload to a temp file and
    runs run_pipeline() in a background task. Returns a job_id immediately.

    Input validation:
      - Allowed types: image/jpeg, image/png, image/webp
      - Max size: 10 MB (rejects before wasting an API call)
      - Corrupt image: decoded with PIL before pipeline starts
    """
    ct = (image.content_type or "").lower()
    ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}

    if ct not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type '{ct}'. "
                f"Only JPEG, PNG, and WebP images are accepted."
            ),
        )

    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    # Stream the file to a temp path, tracking size
    suffix = Path(image.filename or "upload").suffix or ".jpg"
    job_id = uuid.uuid4().hex[:12]
    save_path = UPLOAD_DIR / f"{job_id}{suffix}"

    bytes_read = 0
    with open(save_path, "wb") as out:
        while True:
            chunk = await image.read(64 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_SIZE_BYTES:
                out.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"File is too large ({bytes_read / 1024 / 1024:.1f} MB). "
                        f"Maximum allowed size is 10 MB."
                    ),
                )
            out.write(chunk)

    # Corrupt image detection: try to decode with PIL before running the pipeline
    try:
        from PIL import Image
        with Image.open(save_path) as img:
            img.verify()
        # Re-open after verify (PIL requires it after verify)
        with Image.open(save_path) as img:
            img.load()
    except Exception as exc:
        save_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not decode image — it may be corrupt or not a valid image file. "
                f"PIL error: {exc}"
            ),
        )

    # Create job
    job = JobState(job_id=job_id, image_path=str(save_path))
    with _jobs_lock:
        _jobs[job_id] = job

    # Run the pipeline in a background thread
    threading.Thread(
        target=_run_pipeline_job,
        args=(job_id, str(save_path)),
        daemon=True,
    ).start()

    return {
        "job_id": job_id,
        "status": job.status,
        "stage": job.stage,
        "progress": job.progress,
        "image_path": str(save_path),
    }


@app.get("/api/pipeline/status/{job_id}")
def pipeline_status(job_id: str) -> dict:
    job = _get_job(job_id)
    return job.to_dict()


@app.get("/api/pipeline/result/{job_id}/face-crop")
def pipeline_face_crop(job_id: str):
    """
    Serve the cropped face image saved during detection.

    Allows the frontend to display the detected face without re-running
    detection. The cropped image is saved by InsightFace during Stage 1.

    Returns 404 if the job is not done yet or if no face was detected.
    """
    job = _get_job(job_id)

    # Allow the face crop to be retrieved even when the chain write failed,
    # as long as face detection succeeded (partial-success case).
    if job.status not in ("done", "error"):
        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} is not done yet (status={job.status}). "
            "Wait for the pipeline to finish before fetching the face crop.",
        )

    if job.result is None or job.result.get("face") is None:
        raise HTTPException(
            status_code=404,
            detail=f"No face detected in job {job_id}.",
        )

    crop_path = job.result["face"].get("cropped_face_path")
    if not crop_path:
        raise HTTPException(
            status_code=404,
            detail=f"No cropped face path in job {job_id} result.",
        )

    path = Path(crop_path)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Cropped face file not found: {crop_path}",
        )

    # Detect content type from file extension
    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    media_type = media_types.get(ext, "image/jpeg")

    return FileResponse(str(path), media_type=media_type)


@app.get("/api/pipeline/verify/{record_id}")
def pipeline_verify(
    record_id: int,
    image: str | None = None,
) -> dict:
    """
    Run verify_record() synchronously and return the 3-check report as JSON.
    `image` is an optional query parameter — local path to a face image
    to compare against the record's embedding.
    """
    try:
        result = verify_record(record_id, original_image_path=image)
    except Exception as exc:  # noqa: BLE001
        log.exception("verify failed for record %d", record_id)
        raise HTTPException(status_code=500, detail=f"verify failed: {exc}")

    return {
        "record_id": result.record_id,
        "on_chain": result.on_chain,
        "fetched_record": result.fetched_record,
        "checks": [c.model_dump() for c in result.checks],
        "all_passed": result.all_passed,
        "summary": result.summary,
    }


@app.get("/api/pipeline/proxy-image")
async def image_proxy(url: str) -> FileResponse:
    """
    Proxy an image URL through this server.

    Browsers hotlink-blocking and CORS issues often prevent the frontend
    from loading images from external domains (x.com, instagram.com, etc.).
    This endpoint fetches the image server-side and streams it back so
    the frontend can display it without CORS headers issues.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be http(s)")

    # Whitelist of allowed domains to avoid SSRF
    ALLOWED_DOMAINS = {
        # Twitter / X
        "pbs.twimg.com",
        "abs.twimg.com",
        "platform.twitter.com",
        "x.com",
        "twitter.com",
        # Instagram
        "scontent.cdninstagram.com",
        "scontent-*.cdninstagram.com",
        "instagram.com",
        # Facebook (CDN endpoints only — page URLs return login walls)
        "lookaside.fbsbx.com",
        "platform-lookaside.fbsbx.com",
        "scontent-*.fbcdn.net",
        "scontent.fbcdn.net",
        # LinkedIn
        "linkedin.com",
        "media.licdn.com",
        # Reddit
        "i.redd.it",
        "preview.redd.it",
        "reddit.com",
        # Pinterest
        "i.pinimg.com",
        "pinterest.com",
        # Tumblr
        "static.tumblr.com",
        "tumblr.com",
        # VK
        "vk.com",
        "sun1-*.userapi.com",
        "pp.userapi.com",
        # Other image CDNs commonly returned by Vision API
        "i.imgur.com",
        "i.pinimg.com",
        "upload.wikimedia.org",
        "m.media-amazon.com",
        "storage.googleapis.com",
        "substackcdn.com",
        "images.squarespace-cdn.com",
        "worldhistory.org",
        "1st-art-gallery.com",
        "annamgallery.com",
        "arthistoryproject.com",
        "artchive.com",
        "bloganchoi.com",
        "espressonews.gr",
        "honestabes.info",
        "media.thisisgallery.com",
        "mavenart.com",
        "thetimes.com",
        "thumbs.dreamstime.com",
        "ichef.bbci.co.uk",
    }

    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    def _host_allowed(h: str) -> bool:
        for dom in ALLOWED_DOMAINS:
            if "*" in dom:
                # simple wildcard support: *.example.com
                prefix, suffix = dom.split("*.", 1)
                if h.endswith(suffix) and h.startswith(prefix) and len(h) > len(suffix) + len(prefix):
                    return True
            elif h == dom or h.endswith("." + dom):
                return True
        return False

    if not _host_allowed(host):
        raise HTTPException(
            status_code=403,
            detail=f"Image proxy not allowed for host: {host}",
        )

    # Fetch the image server-side
    try:
        r = requests.get(url, timeout=15, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (faceid-chain-verify/1.0)"
        })
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Image fetch failed: {exc}")

    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream returned {r.status_code} for {url}",
        )

    # Determine a filename + content type
    content_type = r.headers.get("Content-Type", "image/jpeg")
    ext = ".jpg"
    if "png" in content_type:
        ext = ".png"
    elif "gif" in content_type:
        ext = ".gif"
    elif "webp" in content_type:
        ext = ".webp"

    # Save to a temp file and stream back
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        for chunk in r.iter_content(chunk_size=8192):
            tmp.write(chunk)
        tmp.flush()
    finally:
        tmp.close()

    return FileResponse(tmp.name, media_type=content_type)


# Also register the same handler at /api/image-proxy for backward compatibility
app.add_api_route(
    "/api/image-proxy",
    image_proxy,
    methods=["GET"],
    name="image_proxy_alias",
    summary="[Alias] Proxy an external image URL",
    description=(
        "Alias of GET /api/pipeline/proxy-image. "
        "Fetches an image server-side and streams it back, bypassing CORS/hotlink blocks."
    ),
)


# ---------------------------------------------------------------------------
# Direct-run entry (for `python -m app.api.main`)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
