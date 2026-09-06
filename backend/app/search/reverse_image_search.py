"""Reverse image search via Google Cloud Vision Web Detection API."""

import base64
import logging
import os
from pathlib import Path
from typing import Any

import requests

from backend.app.search.exceptions import NoMatchFoundError, VisionAPIError
from backend.app.utils.retry import retry_on_network_error

log = logging.getLogger("search")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"
SOCIAL_DOMAINS = {
    "x.com",
    "twitter.com",
    "instagram.com",
    "facebook.com",
    "linkedin.com",
    "reddit.com",
    "pinterest.com",
    "tumblr.com",
    "vk.com",
}

# Priority order for ranking: full match > partial match > visually similar
_MATCH_TYPE_PRIORITY = {
    "full_match": 0,
    "partial_match": 1,
    "visually_similar": 2,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_api_key() -> str:
    key = os.environ.get("GOOGLE_VISION_API_KEY", "").strip()
    if not key:
        raise EnvironmentError(
            "GOOGLE_VISION_API_KEY is not set. "
            "Copy .env.example to .env and fill in your key."
        )
    return key


def _load_and_encode_image(image_path: str | Path) -> str:
    """Load an image file and return it as a base64-encoded string."""
    path = Path(image_path)
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_url(d: dict) -> str | None:
    """Safely extract the 'url' field from a Vision API response entity dict."""
    return d.get("url") or d.get("source", {}).get("url")


def _parse_title(item: dict) -> str:
    """Safely extract the page title from a Vision API web result item."""
    return item.get("pageTitle", "") or item.get("title", "")


# ---------------------------------------------------------------------------
# Raw response parsers
# ---------------------------------------------------------------------------

def parse_web_detection(raw: dict) -> dict[str, Any]:
    """Parse the 'webAnnotation' or 'webDetection' dict from the API response.

    Handles both legacy 'webAnnotation' field names and the newer
    'webDetection' field names returned by the Vision API.

    Returns a normalised dict with the following keys:
        full_matching_images: list[dict]   — url, pageTitle
        partial_matching_images: list[dict]
        pages_with_matching_images: list[dict]
        best_guess_labels: list[str]
        visually_similar_images: list[dict]
    """
    web = raw.get("webDetection") or raw.get("webAnnotation") or {}

    def _image_items(items: list[dict] | None) -> list[dict]:
        if not items:
            return []
        return [
            {
                "url": _parse_url(item) or "",
                "page_title": _parse_title(item),
            }
            for item in items
            if _parse_url(item)
        ]

    def _page_items(items: list[dict] | None) -> list[dict]:
        if not items:
            return []
        results = []
        for item in items:
            url = _parse_url(item)
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "page_title": _parse_title(item),
                    "matched_image_url": (
                        _parse_url(item.get("image", {}))
                        if isinstance(item.get("image"), dict)
                        else ""
                    ),
                }
            )
        return results

    best_guesses = [
        label.get("label", "")
        for label in web.get("bestGuessLabels", [])
        if label.get("label")
    ]

    return {
        "full_matching_images": _image_items(web.get("fullMatchingImages", [])),
        "partial_matching_images": _image_items(web.get("partialMatchingImages", [])),
        "pages_with_matching_images": _page_items(web.get("pagesWithMatchingImages", [])),
        "best_guess_labels": best_guesses,
        "visually_similar_images": _image_items(web.get("visuallySimilarImages", [])),
    }


# ---------------------------------------------------------------------------
# Vision API HTTP-level errors (for retry logic)
# ---------------------------------------------------------------------------

class _VisionHTTPError(Exception):
    """Wraps HTTP errors from the Vision API for retry detection."""
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


def _handle_vision_api_error(status_code: int, response_body: str | dict) -> None:
    """Inspect error responses and raise a user-facing VisionAPIError.

    Distinguishes quota/rate-limit errors (429) from other failures so callers
    can display a helpful message. Also handles billing-disabled 403s with a
    clear user-facing message.
    """
    # Normalise message extraction
    if isinstance(response_body, dict):
        err = response_body.get("error", {})
        code = err.get("code", 0)
        msg = err.get("message", str(response_body))
    else:
        code = 0
        msg = str(response_body)

    # Billing not enabled is a common 403 cause
    if "billing" in msg.lower() or "billing" in str(response_body).lower():
        raise VisionAPIError(
            status_code,
            (
                "Google Cloud Vision API requires billing to be enabled. "
                "Visit https://console.cloud.google.com/billing to link a billing account "
                "to your project, then retry. "
                f"Details: {msg}"
            ),
            is_rate_limit=False,
        )

    # Google RPC codes: 4=INVALID_ARGUMENT, 7=PERMISSION_DENIED, 8=RESOURCE_EXHAUSTED (quota)
    if code == 8 or "quota" in msg.lower() or "rate" in msg.lower():
        raise VisionAPIError(
            status_code,
            (
                "Google Cloud Vision API quota exceeded. "
                "Visit https://console.cloud.google.com/apis/credentials "
                "to check your quota usage, or enable billing to increase limits. "
                f"Details: {msg}"
            ),
            is_rate_limit=True,
        )
    if code == 7:
        raise VisionAPIError(
            status_code,
            f"Permission denied — check your GOOGLE_VISION_API_KEY is valid. Details: {msg}",
        )
    raise VisionAPIError(status_code, msg)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class ReverseImageSearcher:
    """Client for the Google Cloud Vision Web Detection API.

    Authenticates via the ``GOOGLE_VISION_API_KEY`` environment variable.
    Retries transient HTTP errors with exponential backoff (max 3 attempts).
    """

    def __init__(self, api_url: str = VISION_API_URL):
        self.api_url = api_url
        self._api_key = _extract_api_key()

    @retry_on_network_error(max_attempts=3, base_delay=1.5, max_delay=20.0)
    def search(self, image_path: str | Path) -> dict[str, Any]:
        """Send an image to the Vision API Web Detection endpoint.

        Retries on HTTP 429, 5xx, and connection errors up to 3 times with
        exponential backoff. Quota errors (HTTP 429 / RPC code 8) surface a
        user-friendly message rather than a raw stack trace.

        Parameters
        ----------
        image_path : str | Path
            Path to the image file (JPEG, PNG, GIF, BMP, WEBP).

        Returns
        -------
        dict[str, Any]
            Normalised raw response with keys:
            ``full_matching_images``, ``partial_matching_images``,
            ``pages_with_matching_images``, ``best_guess_labels``,
            ``visually_similar_images``, and the full raw API response
            dict under ``_raw_response``.

        Raises
        ------
        VisionAPIError
            If the API returns a non-200 status or a non-empty error block
            (including quota errors with user-friendly message).
        FileNotFoundError
            If the image file cannot be read.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")

        b64_image = _load_and_encode_image(path)

        payload = {
            "requests": [
                {
                    "image": {"content": b64_image},
                    "features": [{"type": "WEB_DETECTION", "maxResults": 20}],
                }
            ]
        }

        params = {"key": self._api_key}
        try:
            response = requests.post(
                self.api_url,
                params=params,
                json=payload,
                timeout=30,
            )
        except requests.RequestException as exc:
            # Wrap so the retry decorator can detect it
            raise _VisionHTTPError(0, str(exc)) from exc

        if response.status_code != 200:
            # Try to parse as JSON for structured error, fall back to raw text
            try:
                err_body = response.json()
            except Exception:
                err_body = response.text
            _handle_vision_api_error(response.status_code, err_body)

        data = response.json()

        # Propagate API-level errors with user-facing messages
        first_response = data.get("responses", [{}])[0]
        error = first_response.get("error", {})
        if error:
            _handle_vision_api_error(error.get("code", 0), {"error": error})

        parsed = parse_web_detection(first_response)
        parsed["_raw_response"] = data
        return parsed

    def rank_social_matches(self, raw_response: dict[str, Any]) -> list[dict[str, Any]]:
        """Filter and rank web detection results to social-media domains."""
        seen_urls: set[str] = set()
        social_matches: list[dict[str, Any]] = []

        def _add_matches(
            items: list[dict], match_type: str, confidence_note: str
        ) -> None:
            for item in items:
                url = item.get("url", "")
                if not url:
                    continue
                try:
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    domain = parsed.netloc.lower().removeprefix("www.")
                except Exception:
                    domain = ""

                if domain not in SOCIAL_DOMAINS:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                social_matches.append(
                    {
                        "url": url,
                        "domain": domain,
                        "page_title": item.get("page_title", ""),
                        "matched_image_url": item.get("matched_image_url", "") or item.get("url", ""),
                        "match_type": match_type,
                        "confidence_note": confidence_note,
                    }
                )

        for page in raw_response.get("pages_with_matching_images", []):
            _add_matches([page], "full_match", "exact page-level match")
        for img in raw_response.get("full_matching_images", []):
            _add_matches([img], "full_match", "exact image match across web")
        for img in raw_response.get("partial_matching_images", []):
            _add_matches([img], "partial_match", "partial image match")
        for img in raw_response.get("visually_similar_images", []):
            _add_matches([img], "visually_similar", "visually similar image")

        if not social_matches:
            raise NoMatchFoundError(
                "No social-domain matches found in web detection results.",
                raw_result=raw_response,
            )

        social_matches.sort(
            key=lambda m: (
                _MATCH_TYPE_PRIORITY.get(m["match_type"], 99),
                m["url"],
            )
        )
        return social_matches
