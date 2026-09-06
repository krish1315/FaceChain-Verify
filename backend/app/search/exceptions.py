class NoMatchFoundError(Exception):
    """Raised when no matching pages on social domains are found."""

    def __init__(self, message: str | None = None, raw_result: dict | None = None):
        self.raw_result = raw_result
        self.message = message or "No social-domain match found"
        super().__init__(self.message)


class VisionAPIError(Exception):
    """Raised when the Google Cloud Vision API returns an error response."""

    def __init__(
        self,
        status_code: int,
        response_body: dict | str,
        is_rate_limit: bool = False,
    ):
        self.status_code = status_code
        self.response_body = response_body
        self.is_rate_limit = is_rate_limit
        self.message = f"Google Vision API error {status_code}: {response_body}"
        super().__init__(self.message)
