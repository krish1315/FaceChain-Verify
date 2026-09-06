"""Tests for the reverse image search module.

These tests make REAL API calls to Google Cloud Vision — they require
GOOGLE_VISION_API_KEY to be set in the environment (loaded from .env
if present). They are skipped if the key is missing.
"""

import os
import sys
from pathlib import Path

# Ensure backend/ is on sys.path so the test file can be run directly
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pytest
from dotenv import load_dotenv

# Load .env from the project root if present
PROJECT_ROOT = BACKEND_DIR.parent
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

from backend.app.search.exceptions import (  # noqa: E402
    NoMatchFoundError,
    VisionAPIError,
)
from backend.app.search.reverse_image_search import (  # noqa: E402
    ReverseImageSearcher,
    parse_web_detection,
    SOCIAL_DOMAINS,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FACE_IMAGE = FIXTURES_DIR / "mona_lisa.jpg"


def _has_api_key() -> bool:
    return bool(os.environ.get("GOOGLE_VISION_API_KEY", "").strip())


@pytest.mark.skipif(not _has_api_key(), reason="GOOGLE_VISION_API_KEY not set")
def test_search_returns_pages_with_matching_images():
    """A well-known public-domain image should return at least one page match."""
    searcher = ReverseImageSearcher()
    result = searcher.search(FACE_IMAGE)

    # The Vision API response should be parsed cleanly
    assert isinstance(result, dict)
    assert "pages_with_matching_images" in result
    assert "best_guess_labels" in result
    assert "full_matching_images" in result
    assert "partial_matching_images" in result
    assert "visually_similar_images" in result

    # Should have at least one page-level match
    pages = result["pages_with_matching_images"]
    assert len(pages) >= 1, f"Expected at least 1 page, got {len(pages)}"

    # Best guess labels should be present
    assert isinstance(result["best_guess_labels"], list)

    print(f"\n[test_search_returns_pages_with_matching_images]")
    print(f"  best_guess_labels: {result['best_guess_labels']}")
    print(f"  pages_with_matching_images: {len(pages)}")
    print(f"  full_matching_images: {len(result['full_matching_images'])}")
    print(f"  partial_matching_images: {len(result['partial_matching_images'])}")
    print(f"  visually_similar_images: {len(result['visually_similar_images'])}")

    for i, p in enumerate(pages[:3]):
        print(f"  page {i}: {p.get('page_title','')[:60]!r} -> {p.get('url','')[:80]}")


@pytest.mark.skipif(not _has_api_key(), reason="GOOGLE_VISION_API_KEY not set")
def test_rank_social_matches_filters_and_sorts():
    """rank_social_matches should filter to social domains and rank by match type."""
    searcher = ReverseImageSearcher()
    result = searcher.search(FACE_IMAGE)

    # Whether or not there are social matches, the method should behave correctly.
    # Try to rank; if nothing social found, verify we get NoMatchFoundError
    # with the raw result attached.
    try:
        ranked = searcher.rank_social_matches(result)
    except NoMatchFoundError as exc:
        # Even if no social match, raw result should be attached
        assert exc.raw_result is not None
        assert "pages_with_matching_images" in exc.raw_result
        print(f"\n[test_rank_social_matches_filters_and_sorts]")
        print(f"  No social matches (NoMatchFoundError).")
        print(f"  best_guess_labels: {exc.raw_result.get('best_guess_labels', [])}")
        return  # Test passes — error was raised correctly

    # If we did find social matches, verify structure & ordering
    assert isinstance(ranked, list)
    assert len(ranked) >= 1
    for m in ranked:
        assert "url" in m
        assert "domain" in m
        assert "page_title" in m
        assert "matched_image_url" in m
        assert "match_type" in m
        assert "confidence_note" in m
        # Domain must be a known social domain
        assert m["domain"] in SOCIAL_DOMAINS, f"unexpected domain: {m['domain']}"
        # Match type must be one of the three
        assert m["match_type"] in {"full_match", "partial_match", "visually_similar"}

    # Sort order check: full_match must come before partial_match, etc.
    type_order = {"full_match": 0, "partial_match": 1, "visually_similar": 2}
    for i in range(len(ranked) - 1):
        a, b = ranked[i], ranked[i + 1]
        assert type_order[a["match_type"]] <= type_order[b["match_type"]], (
            f"Bad sort order: {a['match_type']} after {b['match_type']}"
        )

    print(f"\n[test_rank_social_matches_filters_and_sorts]")
    print(f"  Top 3 ranked social matches:")
    for i, m in enumerate(ranked[:3]):
        print(
            f"    {i+1}. [{m['match_type']}] {m['domain']} "
            f"-> {m['url']}"
        )
        if m.get("page_title"):
            print(f"       title: {m['page_title'][:70]!r}")


def test_parse_web_detection_handles_empty_response():
    """parse_web_detection should handle a missing/empty webDetection block."""
    parsed = parse_web_detection({})
    assert parsed == {
        "full_matching_images": [],
        "partial_matching_images": [],
        "pages_with_matching_images": [],
        "best_guess_labels": [],
        "visually_similar_images": [],
    }


def test_parse_web_detection_handles_full_response():
    """parse_web_detection should normalise a synthetic full response."""
    sample = {
        "webDetection": {
            "bestGuessLabels": [{"label": "Mona Lisa"}],
            "fullMatchingImages": [
                {"url": "https://example.com/a.jpg"},
                {"url": "https://example.com/b.jpg"},
            ],
            "partialMatchingImages": [
                {"url": "https://example.com/c.jpg", "pageTitle": "Partial"},
            ],
            "pagesWithMatchingImages": [
                {
                    "url": "https://www.instagram.com/p/abc",
                    "pageTitle": "Instagram post",
                    "image": {"url": "https://example.com/d.jpg"},
                }
            ],
            "visuallySimilarImages": [
                {"url": "https://example.com/e.jpg"},
            ],
        }
    }
    parsed = parse_web_detection(sample)
    assert parsed["best_guess_labels"] == ["Mona Lisa"]
    assert len(parsed["full_matching_images"]) == 2
    assert parsed["partial_matching_images"][0]["page_title"] == "Partial"
    # parse_web_detection does NOT extract domain — that happens in rank_social_matches
    assert parsed["pages_with_matching_images"][0]["url"] == (
        "https://www.instagram.com/p/abc"
    )
    assert parsed["pages_with_matching_images"][0]["matched_image_url"] == (
        "https://example.com/d.jpg"
    )


def test_rank_social_matches_raises_when_no_social_match():
    """rank_social_matches should raise NoMatchFoundError when nothing social."""
    from backend.app.search.exceptions import NoMatchFoundError as NF

    fake_response = {
        "pages_with_matching_images": [
            {"url": "https://example.com/foo", "page_title": "Foo"}
        ],
        "full_matching_images": [],
        "partial_matching_images": [],
        "visually_similar_images": [],
        "best_guess_labels": ["Some label"],
    }
    searcher = ReverseImageSearcher.__new__(ReverseImageSearcher)
    with pytest.raises(NF) as excinfo:
        searcher.rank_social_matches(fake_response)
    # Raw result should be attached
    assert excinfo.value.raw_result is fake_response


if __name__ == "__main__":
    # Allow running directly: python backend/tests/test_search.py
    pytest.main([__file__, "-v", "-s"])
