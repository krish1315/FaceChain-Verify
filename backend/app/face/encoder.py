"""Face detection and embedding extraction using InsightFace (buffalo_l)."""

import os
import tempfile
from pathlib import Path

import cv2
import numpy as np

from backend.app.face.exceptions import FaceNotFoundError

# InsightFace imports
from insightface.app import FaceAnalysis
from insightface.app.common import Face

# Lazy-initialized model handle
_face_app: FaceAnalysis | None = None


def _get_face_app() -> FaceAnalysis:
    """Return a lazily-initialized FaceAnalysis instance (buffalo_l, CPU)."""
    global _face_app
    if _face_app is None:
        _face_app = FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        _face_app.prepare(ctx_id=0)
    return _face_app


class FaceEncoder:
    """Detects faces and extracts 512-d embeddings using InsightFace.

    Attributes:
        model_name: InsightFace model name (default: "buffalo_l").
        device: Execution device (default: "CPUExecutionProvider").
    """

    def __init__(self, model_name: str = "buffalo_l", device: str = "CPUExecutionProvider"):
        self.model_name = model_name
        self.device = device
        self._app: FaceAnalysis | None = None

    @property
    def app(self) -> FaceAnalysis:
        """Return the lazily-initialized FaceAnalysis instance."""
        if self._app is None:
            self._app = FaceAnalysis(name=self.model_name, providers=[self.device])
            self._app.prepare(ctx_id=0)
        return self._app

    def detect_faces(self, image_path: str | Path) -> list[dict]:
        """Detect faces in an image.

        Args:
            image_path: Path to the input image (JPEG/PNG).

        Returns:
            List of dicts with keys:
                - bbox: list[int] — [x1, y1, x2, y2] bounding box
                - landmarks: list[list[int]] — 5-point facial landmarks
                - confidence: float — detection confidence score

        Raises:
            FaceNotFoundError: If no face is detected.
        """
        image_path = Path(image_path)
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")

        faces = self.app.get(img)
        if not faces:
            raise FaceNotFoundError(str(image_path))

        results = []
        for face in faces:
            # Some faces (e.g. paintings, low-quality crops) may have no
            # 5-point landmarks. Fall back to None or an empty list.
            landmark = face.landmark
            if landmark is None or len(landmark) == 0:
                landmarks: list[list[int]] = []
            else:
                landmarks = [[int(x), int(y)] for x, y in landmark]
            results.append(
                {
                    "bbox": [int(v) for v in face.bbox],
                    "landmarks": landmarks,
                    "confidence": float(face.det_score),
                }
            )
        return results

    def encode(
        self, image_path: str | Path
    ) -> tuple[np.ndarray, str, float]:
        """Extract the 512-d embedding of the largest/highest-confidence face.

        Args:
            image_path: Path to the input image.

        Returns:
            Tuple of (embedding, cropped_face_path, confidence_score):
                - embedding: numpy.ndarray of shape (512,) float32
                - cropped_face_path: path to the saved cropped face image
                - confidence_score: float detection confidence

        Raises:
            FaceNotFoundError: If no face is detected.
            FileNotFoundError: If the image cannot be read.
        """
        image_path = Path(image_path)
        img = cv2.imread(str(image_path))
        if img is None:
            raise FileNotFoundError(f"Could not read image: {image_path}")

        faces = self.app.get(img)
        if not faces:
            raise FaceNotFoundError(str(image_path))

        # Select the largest face (by bounding box area) as the primary face
        def _face_area(face: Face) -> float:
            bbox = face.bbox
            return float((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))

        primary_face = max(faces, key=_face_area)

        # Crop the face region for the saved temp image
        x1, y1, x2, y2 = [int(v) for v in primary_face.bbox]
        # Add padding to keep the crop within image bounds
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(img.shape[1], x2)
        y2 = min(img.shape[0], y2)
        cropped = img[y1:y2, x1:x2]

        # Save cropped face to a temp file
        tmp_dir = tempfile.mkdtemp(prefix="faceid_face_")
        cropped_path = os.path.join(tmp_dir, "face_crop.jpg")
        cv2.imwrite(cropped_path, cropped)

        embedding = primary_face.embedding
        confidence = float(primary_face.det_score)

        return embedding, cropped_path, confidence

    def encode_and_print(self, image_path: str | Path) -> None:
        """Convenience method that prints detection details for the successful case."""
        embedding, cropped_path, confidence = self.encode(image_path)
        print(f"Embedding shape: {embedding.shape}")
        print(f"Detection confidence: {confidence:.4f}")
        print(f"Cropped face saved to: {cropped_path}")
        return embedding, cropped_path, confidence