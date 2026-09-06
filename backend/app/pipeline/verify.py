"""
verify_record — tamper-evident verification of an anchored record.

Reads a record from the on-chain registry, fetches the pinned JSON from
IPFS, recomputes the SHA-256 hash, and (optionally) verifies the
embedding hash matches an input image's embedding.
"""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import pydantic
import requests

from backend.app.chain.contract_client import ContractClient
from backend.app.face.encoder import FaceEncoder
from backend.app.face.utils import embedding_to_hash
from backend.app.face.exceptions import FaceNotFoundError

log = logging.getLogger("verify")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)


# IPFS public gateways (try in order)
IPFS_GATEWAYS = [
    "https://gateway.pinata.cloud/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
    "https://dweb.link/ipfs/{cid}",
]


class CheckResult(pydantic.BaseModel):
    name: str
    passed: bool
    detail: str = ""


class VerifyResult(pydantic.BaseModel):
    record_id: int
    on_chain: dict[str, Any] | None = None
    fetched_record: dict[str, Any] | None = None
    checks: list[CheckResult] = []
    all_passed: bool = False

    @property
    def summary(self) -> str:
        lines = [
            f"Record ID: {self.record_id}",
            "",
            "Checks:",
        ]
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"  [{mark}]  {c.name}: {c.detail}")
        lines.append("")
        lines.append(f"Overall: {'PASS — all checks succeeded' if self.all_passed else 'FAIL — see above'}")
        return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fetch_ipfs_raw(cid: str, timeout: int = 30) -> bytes | None:
    """Fetch raw bytes from IPFS via public gateways.

    We fetch the raw bytes (not parsed JSON) so we can hash the exact same
    byte sequence that was originally pinned. Re-serialising the parsed JSON
    object could produce a different byte sequence (e.g. Unicode codepoints
    encoded as escape sequences vs literal bytes), causing the hash to mismatch.
    """
    for gw in IPFS_GATEWAYS:
        url = gw.format(cid=cid)
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content
            log.warning("Gateway %s returned %d for %s", url, r.status_code, cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Gateway %s failed: %s", url, exc)
    return None


def _fetch_ipfs_json(cid: str, timeout: int = 30) -> dict | None:
    """Fetch JSON from IPFS via public gateways, returning the first success."""
    for gw in IPFS_GATEWAYS:
        url = gw.format(cid=cid)
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            log.warning("Gateway %s returned %d for %s", url, r.status_code, cid)
        except Exception as exc:  # noqa: BLE001
            log.warning("Gateway %s failed: %s", url, exc)
    return None


# ─── Main verify function ─────────────────────────────────────────────────────

def verify_record(
    record_id: int,
    original_image_path: str | Path | None = None,
) -> VerifyResult:
    """
    Verify a record on the chain against its pinned IPFS content.

    Parameters
    ----------
    record_id : int
        The on-chain record id returned by submit_record.
    original_image_path : str | Path, optional
        If given, re-encode this image and compare its face embedding
        against the embedding hash stored in the fetched IPFS record.

    Returns
    -------
    VerifyResult
        Contains on-chain record, fetched record, and per-check results.
    """
    result = VerifyResult(record_id=record_id)

    client = ContractClient()

    # ── Check 1: record exists on chain ───────────────────────────────────
    try:
        on_chain = client.get_record(record_id)
    except Exception as exc:  # noqa: BLE001
        # Likely a non-existent record id
        result.checks.append(
            CheckResult(
                name="record_exists_on_chain",
                passed=False,
                detail=f"Could not read record {record_id} from chain: {exc}",
            )
        )
        log.error("Verification FAILED: record %d does not exist on chain", record_id)
        return result

    if on_chain.get("recordHash") == "0x" + "00" * 32 or not on_chain.get("recordHash"):
        result.checks.append(
            CheckResult(
                name="record_exists_on_chain",
                passed=False,
                detail="On-chain recordHash is empty (record id unused).",
            )
        )
        return result

    result.on_chain = on_chain
    result.checks.append(
        CheckResult(
            name="record_exists_on_chain",
            passed=True,
            detail=(
                f"Found at {client.contract_address}; "
                f"submitter={on_chain['submitter']}; "
                f"timestamp={on_chain['timestamp']}"
            ),
        )
    )
    log.info(
        "Check [record_exists_on_chain]  PASS  submitter=%s  ts=%d",
        on_chain["submitter"], on_chain["timestamp"]
    )

    # ── Check 2: chain-vs-IPFS integrity ──────────────────────────────────
    on_chain_hash = on_chain["recordHash"]  # 0x-prefixed hex
    on_chain_cid = on_chain["ipfsCID"]

    # Fetch RAW bytes (not re-serialised JSON) so the hash matches exactly what
    # was computed at submission time — avoids Unicode encoding mismatches.
    raw_bytes = _fetch_ipfs_raw(on_chain_cid)
    if raw_bytes is None:
        result.checks.append(
            CheckResult(
                name="chain_vs_ipfs_integrity",
                passed=False,
                detail=f"Could not fetch CID {on_chain_cid} from any IPFS gateway",
            )
        )
        log.error("Verification FAILED: IPFS fetch failed for %s", on_chain_cid)
        return result

    # Also parse as JSON for the face embedding check (stored record field)
    fetched = _fetch_ipfs_json(on_chain_cid)
    result.fetched_record = fetched

    # Hash the exact raw bytes from IPFS — matches what was submitted to Pinata
    computed_hash_bytes = hashlib.sha256(raw_bytes).digest()
    computed_hash_hex = "0x" + computed_hash_bytes.hex()

    chain_hash_bytes_no0x = on_chain_hash.removeprefix("0x").lower()
    computed_hash_no0x = computed_hash_hex.removeprefix("0x").lower()

    integrity_ok = chain_hash_bytes_no0x == computed_hash_no0x
    result.checks.append(
        CheckResult(
            name="chain_vs_ipfs_integrity",
            passed=integrity_ok,
            detail=(
                f"on-chain hash: {on_chain_hash[:20]}…\n"
                f"                sha256(canonical fetch): {computed_hash_hex[:20]}…\n"
                f"                match: {integrity_ok}"
            ),
        )
    )
    log.info(
        "Check [chain_vs_ipfs_integrity]  %s",
        "PASS" if integrity_ok else "FAIL"
    )

    # ── Check 3 (optional): face embedding match ──────────────────────────
    if original_image_path is not None:
        image_path = Path(original_image_path)
        if not image_path.exists():
            result.checks.append(
                CheckResult(
                    name="face_embedding_match",
                    passed=False,
                    detail=f"Image not found: {image_path}",
                )
            )
        else:
            try:
                encoder = FaceEncoder()
                embedding, _, _ = encoder.encode(image_path)
                local_hash = embedding_to_hash(embedding)

                # The on-chain / IPFS record stores the embedding hash as a
                # plain 64-char hex string (no 0x prefix). Compare accordingly.
                stored_emb_hash = fetched.get("faceEmbeddingHash", "").strip().lower()
                local_hash_clean = local_hash.strip().lower()

                # `local_hash` is also a 64-char hex string (no 0x)
                face_ok = stored_emb_hash == local_hash_clean
                result.checks.append(
                    CheckResult(
                        name="face_embedding_match",
                        passed=face_ok,
                        detail=(
                            f"local image embedding hash:  {local_hash[:24]}…\n"
                            f"                stored in record: {stored_emb_hash[:24]}…\n"
                            f"                match: {face_ok}"
                        ),
                    )
                )
                log.info(
                    "Check [face_embedding_match]   %s",
                    "PASS" if face_ok else "FAIL"
                )
            except FaceNotFoundError as exc:
                result.checks.append(
                    CheckResult(
                        name="face_embedding_match",
                        passed=False,
                        detail=f"FaceNotFoundError on local image: {exc}",
                    )
                )
                log.error("Check [face_embedding_match]   FAIL  no face in %s", image_path)

    # ── Summary ──────────────────────────────────────────────────────────
    result.all_passed = all(c.passed for c in result.checks)
    log.info("\n%s", result.summary)
    return result
