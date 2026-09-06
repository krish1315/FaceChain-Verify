class FaceNotFoundError(Exception):
    """Raised when no face is detected in the input image."""

    def __init__(self, image_path: str, message: str | None = None):
        self.image_path = image_path
        self.message = message or f"No face detected in image: {image_path}"
        super().__init__(self.message)
