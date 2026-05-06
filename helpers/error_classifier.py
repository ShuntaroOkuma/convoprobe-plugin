"""Classify Reverse Invocation and Backend HTTP errors so the run loop
can decide retry vs. abort consistently.

The Dify Plugin SDK does not expose typed exceptions for
`session.app.chat.invoke`; it raises bare `Exception` whose `str(e)`
contains the underlying message. We pattern-match on that string —
brittle by nature, so the categories are deliberately coarse and the
default is "retry as INTERNAL".

References:
- Architecture §7.1 (error categories + retry policy)
- Day 2 verification: actual SDK error strings observed during /verify
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .backend_client import BackendClientError


class ErrorCategory(str, Enum):
    """Coarse error buckets that drive retry policy.

    String values match the codes Backend may surface to the Web UI in a
    future iteration; keeping them as strings now avoids a churn later.
    """

    INVALID_APP_ID = "INVALID_APP_ID"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    BACKEND_UNREACHABLE = "BACKEND_UNREACHABLE"
    AUTH_FAILED = "AUTH_FAILED"
    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class Classification:
    """The classifier's verdict on a single failure.

    `retriable` reflects architecture §7.2: AUTH_FAILED, INVALID_APP_ID,
    PERMISSION_DENIED are user-fixable mistakes — retrying just burns
    quota and delays the inevitable failure surface in the Web UI.
    """

    category: ErrorCategory
    retriable: bool
    message: str


# Lowercased substrings used for cheap pattern matching. Order matters
# only when two patterns could match the same message; the most specific
# bucket is checked first inside `classify`.
_PATTERNS: tuple[tuple[str, ErrorCategory], ...] = (
    ("app not found", ErrorCategory.INVALID_APP_ID),
    ("invalid app", ErrorCategory.INVALID_APP_ID),
    ("permission denied", ErrorCategory.PERMISSION_DENIED),
    ("not authorized", ErrorCategory.PERMISSION_DENIED),
    ("forbidden", ErrorCategory.PERMISSION_DENIED),
    ("rate limit", ErrorCategory.RATE_LIMITED),
    ("too many requests", ErrorCategory.RATE_LIMITED),
    ("timeout", ErrorCategory.BACKEND_TIMEOUT),
    ("deadline exceeded", ErrorCategory.BACKEND_TIMEOUT),
    ("connection refused", ErrorCategory.BACKEND_UNREACHABLE),
    ("no such host", ErrorCategory.BACKEND_UNREACHABLE),
    ("name resolution", ErrorCategory.BACKEND_UNREACHABLE),
)

_NON_RETRIABLE: frozenset[ErrorCategory] = frozenset(
    {
        ErrorCategory.INVALID_APP_ID,
        ErrorCategory.PERMISSION_DENIED,
        ErrorCategory.AUTH_FAILED,
    }
)


def classify(exc: BaseException) -> Classification:
    """Inspect an exception raised during Reverse Invocation or Backend
    HTTP and return the policy verdict.

    BackendClientError carries an HTTP status, so we check status first
    (more reliable than text matching) before falling through to the
    pattern heuristics. Unknown errors classify as INTERNAL/retriable —
    this matches §7.1 ("default → INTERNAL with backoff").
    """
    if isinstance(exc, BackendClientError):
        category = _classify_status(exc.status)
        if category is not None:
            return Classification(
                category=category,
                retriable=category not in _NON_RETRIABLE,
                message=str(exc),
            )

    text = str(exc).lower()
    for needle, category in _PATTERNS:
        if needle in text:
            return Classification(
                category=category,
                retriable=category not in _NON_RETRIABLE,
                message=str(exc),
            )

    return Classification(
        category=ErrorCategory.INTERNAL,
        retriable=True,
        message=str(exc),
    )


def _classify_status(status: int) -> ErrorCategory | None:
    """Map an HTTP status code to a category. Returns None for codes that
    don't carry useful policy signal (e.g., 200 — caller wouldn't ask).
    """
    if status == 0:
        return ErrorCategory.BACKEND_UNREACHABLE
    if status == 401:
        return ErrorCategory.AUTH_FAILED
    if status == 403:
        return ErrorCategory.PERMISSION_DENIED
    if status == 404:
        return ErrorCategory.INVALID_APP_ID
    if status in (408, 504):
        return ErrorCategory.BACKEND_TIMEOUT
    if status == 429:
        return ErrorCategory.RATE_LIMITED
    if status >= 500:
        return ErrorCategory.INTERNAL
    if status >= 400:
        # Any other 4xx is a client mistake we should not blindly retry.
        return ErrorCategory.AUTH_FAILED
    return None


# Retry schedule from architecture §7.2: exponential backoff with jitter
# is added by the caller to avoid thundering herds when many runs hit the
# same Dify rate limit at once.
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
"""Sleep durations between retry attempts. Length implies max retries."""
