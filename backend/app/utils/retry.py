"""
Retry utilities with exponential backoff for external API calls.

Usage:
    @retry_on_network_error(max_attempts=3, base_delay=1.0)
    def my_api_call():
        ...
"""

import logging
import time
from functools import wraps
from typing import Callable, TypeVar

log = logging.getLogger("retry")
T = TypeVar("T")


class RetryExhaustedError(Exception):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"All {attempts} attempts exhausted. Last error: {last_error}"
        )


def _is_retryable(exc: Exception) -> bool:
    """
    Return True for transient network/HTTP errors that are worth retrying.
    Does NOT retry business-logic errors (4xx other than 429/502/503/504).
    """
    msg = str(exc).lower()
    # Connection-level errors
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    # HTTP 429 Rate Limited
    if getattr(exc, "status_code", None) == 429:
        return True
    # HTTP 5xx Server errors
    sc = getattr(exc, "status_code", 0)
    if 500 <= sc < 600:
        return True
    # Generic network errors in the message
    if any(k in msg for k in ("connection", "timeout", "network", "econnreset", "econnrefused")):
        return True
    # Explicit 403 billing/auth errors — fail fast with a clear message
    if sc == 403:
        if any(k in msg for k in ("billing", "permission", "denied", "scope")):
            return False  # business logic error — don't waste retries
    return False


def retry_on_network_error(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator that retries a function on transient network errors.

    Uses exponential backoff with optional jitter.
    Logs each attempt at WARNING level, and logs final exhaustion at ERROR.

    Args:
        max_attempts: Maximum number of attempts (must be >= 1).
        base_delay:   Initial delay in seconds before the first retry.
        max_delay:   Maximum delay cap between attempts.
        jitter:       Add ±20% random jitter to prevent thundering herd.
    """
    import random

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:
                    if attempt == max_attempts:
                        log.error(
                            "❌  %s  EXHAUSTED after %d attempts — %s: %s",
                            fn.__name__,
                            max_attempts,
                            type(exc).__name__,
                            exc,
                        )
                        raise RetryExhaustedError(max_attempts, exc) from exc

                    if not _is_retryable(exc):
                        # Non-retryable (e.g. bad API key, invalid params) — fail fast
                        log.error(
                            "✗   %s  NON-RETRYABLE on attempt %d — %s: %s",
                            fn.__name__, attempt, type(exc).__name__, exc,
                        )
                        raise

                    # Compute delay: exponential backoff
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    if jitter:
                        delay = delay * (0.8 + 0.4 * random.random())

                    log.warning(
                        "⚠️  %s  attempt %d/%d FAILED — %s: %s — retrying in %.1fs…",
                        fn.__name__, attempt, max_attempts, type(exc).__name__, exc, delay
                    )
                    time.sleep(delay)

            # unreachable, but satisfy type checker
            raise RetryExhaustedError(max_attempts, RuntimeError("unreachable"))

        return wrapper
    return decorator
