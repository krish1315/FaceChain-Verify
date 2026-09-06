"""Utility functions for face encoding module."""

import hashlib
from pathlib import Path

import numpy as np


def sha256_file(path: str | Path) -> str:
    """Compute SHA-256 hash of file contents.

    Args:
        path: Path to the file to hash.

    Returns:
        Hexadecimal string of the SHA-256 hash.
    """
    path = Path(path)
    sha256_hash = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256_hash.update(chunk)
    return sha256_hash.hexdigest()


def embedding_to_hash(embedding: np.ndarray) -> str:
    """Compute SHA-256 hash of a face embedding for tamper evidence.

    Rounds the embedding values and converts to bytes before hashing.
    This provides deterministic, tamper-evident identification.

    Args:
        embedding: 512-d numpy array (face embedding).

    Returns:
        Hexadecimal string of the SHA-256 hash.
    """
    # Round to integers and pack as bytes for deterministic hashing
    rounded = np.round(embedding).astype(np.int16)
    bytes_data = rounded.tobytes()
    return hashlib.sha256(bytes_data).hexdigest()