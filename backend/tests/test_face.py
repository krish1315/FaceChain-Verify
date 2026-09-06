"""Tests for the face encoding module."""

from pathlib import Path

import numpy as np
import pytest

from backend.app.face.encoder import FaceEncoder
from backend.app.face.exceptions import FaceNotFoundError
from backend.app.face.utils import embedding_to_hash, sha256_file

# Path to test fixtures
FIXTURES_DIR = Path(__file__).parent / "fixtures"
FACE_IMAGE = FIXTURES_DIR / "mona_lisa.jpg"
NO_FACE_IMAGE = FIXTURES_DIR / "landscape.jpg"


def test_face_encoder_singleton_lazy_loads():
    """The encoder should construct without error and lazily load the model."""
    encoder = FaceEncoder()
    assert encoder.model_name == "buffalo_l"
    assert encoder.device == "CPUExecutionProvider"
    # App should be lazy — initially None
    assert encoder._app is None


def test_encode_successful_face():
    """Test that a clear face is detected and a 512-d embedding is returned."""
    encoder = FaceEncoder()

    embedding, cropped_path, confidence = encoder.encode(FACE_IMAGE)

    # Verify embedding shape and type
    assert isinstance(embedding, np.ndarray), "Embedding must be a numpy array"
    assert embedding.shape == (512,), f"Embedding must be 512-d, got {embedding.shape}"
    assert embedding.dtype == np.float32, f"Embedding dtype must be float32, got {embedding.dtype}"

    # Verify cropped face path exists
    assert Path(cropped_path).exists(), f"Cropped face file not found: {cropped_path}"

    # Verify detection confidence is reasonable
    assert 0.0 < confidence <= 1.0, f"Confidence should be in (0, 1], got {confidence}"
    assert confidence > 0.3, f"Confidence too low: {confidence}"

    # Print details
    print(f"\n[test_encode_successful_face]")
    print(f"  Embedding shape: {embedding.shape}")
    print(f"  Detection confidence: {confidence:.4f}")
    print(f"  Cropped face saved to: {cropped_path}")


def test_detect_faces_returns_list():
    """Test that detect_faces returns a list of face dictionaries."""
    encoder = FaceEncoder()
    faces = encoder.detect_faces(FACE_IMAGE)

    assert isinstance(faces, list)
    assert len(faces) >= 1

    face = faces[0]
    assert "bbox" in face
    assert "landmarks" in face
    assert "confidence" in face
    assert len(face["bbox"]) == 4
    # Landmarks may be empty for some images (paintings, low-quality).
    # When present, expect 5-point landmarks.
    assert isinstance(face["landmarks"], list)
    if face["landmarks"]:
        assert len(face["landmarks"]) == 5
    assert 0.0 < face["confidence"] <= 1.0

    # Print details
    print(f"\n[test_detect_faces_returns_list]")
    print(f"  Number of faces detected: {len(faces)}")
    print(f"  Bounding box: {face['bbox']}")
    print(f"  Confidence: {face['confidence']:.4f}")


def test_face_not_found_raises_error():
    """Test that an image with no face raises FaceNotFoundError."""
    encoder = FaceEncoder()

    with pytest.raises(FaceNotFoundError) as excinfo:
        encoder.encode(NO_FACE_IMAGE)

    assert str(NO_FACE_IMAGE) in str(excinfo.value) or "no face" in str(excinfo.value).lower()
    print(f"\n[test_face_not_found_raises_error]")
    print(f"  Got expected FaceNotFoundError: {excinfo.value.message}")


def test_sha256_file_helper():
    """Test the sha256_file utility."""
    hash1 = sha256_file(FACE_IMAGE)
    hash2 = sha256_file(FACE_IMAGE)

    # Should be deterministic
    assert hash1 == hash2
    # Should be a 64-character hex string (SHA-256 hex)
    assert len(hash1) == 64
    assert all(c in "0123456789abcdef" for c in hash1)

    # Different file should produce different hash
    hash3 = sha256_file(NO_FACE_IMAGE)
    assert hash1 != hash3


def test_embedding_to_hash_deterministic():
    """Test that embedding_to_hash is deterministic for the same embedding."""
    encoder = FaceEncoder()
    embedding, _, _ = encoder.encode(FACE_IMAGE)

    hash1 = embedding_to_hash(embedding)
    hash2 = embedding_to_hash(embedding)

    assert hash1 == hash2
    assert len(hash1) == 64


def test_embedding_to_hash_differs_for_different_embeddings():
    """Test that different embeddings produce different hashes."""
    fake_embedding = np.random.randn(512).astype(np.float32)
    hash1 = embedding_to_hash(fake_embedding)

    # Add a perturbation
    fake_embedding2 = fake_embedding + 0.1
    hash2 = embedding_to_hash(fake_embedding2)

    assert hash1 != hash2


if __name__ == "__main__":
    # Allow running directly: python backend/tests/test_face.py
    pytest.main([__file__, "-v", "-s"])
