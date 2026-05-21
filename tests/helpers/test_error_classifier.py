"""Unit tests for helpers/error_classifier.classify.

Pattern matching is brittle by nature — these tests pin the
buckets we documented in architecture §7.1 so a future regex tweak
can't silently reclassify "permission denied" as retriable.
"""
from __future__ import annotations

import pytest

from helpers.backend_client import BackendClientError
from helpers.error_classifier import (
    RETRY_BACKOFF_SECONDS,
    Classification,
    ErrorCategory,
    classify,
)


# --- HTTP status mapping (BackendClientError) -------------------------------

@pytest.mark.parametrize(
    ("status", "expected_category", "expected_retriable"),
    [
        (0, ErrorCategory.BACKEND_UNREACHABLE, True),  # transport error
        (401, ErrorCategory.AUTH_FAILED, False),
        (403, ErrorCategory.PERMISSION_DENIED, False),
        (404, ErrorCategory.INVALID_APP_ID, False),
        (408, ErrorCategory.BACKEND_TIMEOUT, True),
        (429, ErrorCategory.RATE_LIMITED, True),
        (500, ErrorCategory.INTERNAL, True),
        (502, ErrorCategory.INTERNAL, True),
        (503, ErrorCategory.INTERNAL, True),
        (504, ErrorCategory.BACKEND_TIMEOUT, True),
        (400, ErrorCategory.AUTH_FAILED, False),  # any other 4xx ⇒ no blind retry
        (422, ErrorCategory.AUTH_FAILED, False),
    ],
)
def test_classify_backend_status(status: int, expected_category: ErrorCategory, expected_retriable: bool):
    exc = BackendClientError("boom", status=status, body="")
    verdict = classify(exc)
    assert verdict.category == expected_category
    assert verdict.retriable is expected_retriable


# --- text-pattern fallback (Reverse Invocation bare Exception) --------------

@pytest.mark.parametrize(
    ("text", "expected_category", "expected_retriable"),
    [
        ("App not found: abc-123", ErrorCategory.INVALID_APP_ID, False),
        ("invalid app id", ErrorCategory.INVALID_APP_ID, False),
        ("permission denied for app", ErrorCategory.PERMISSION_DENIED, False),
        ("Caller is not authorized", ErrorCategory.PERMISSION_DENIED, False),
        ("403 Forbidden", ErrorCategory.PERMISSION_DENIED, False),
        ("Rate limit exceeded", ErrorCategory.RATE_LIMITED, True),
        ("HTTP 429 too many requests", ErrorCategory.RATE_LIMITED, True),
        ("read timeout while waiting for chatflow", ErrorCategory.BACKEND_TIMEOUT, True),
        ("context deadline exceeded", ErrorCategory.BACKEND_TIMEOUT, True),
        ("connection refused by upstream", ErrorCategory.BACKEND_UNREACHABLE, True),
        ("dns: no such host convoprobe.local", ErrorCategory.BACKEND_UNREACHABLE, True),
        ("name resolution error", ErrorCategory.BACKEND_UNREACHABLE, True),
    ],
)
def test_classify_text_patterns(text: str, expected_category: ErrorCategory, expected_retriable: bool):
    exc = Exception(text)
    verdict = classify(exc)
    assert verdict.category == expected_category
    assert verdict.retriable is expected_retriable


@pytest.mark.parametrize(
    ("text", "expected_category"),
    [
        # Real-world Dify daemon reply observed on 2026-05-21 when calling
        # session.app.chat.invoke against a Chatflow / Workflow target —
        # the daemon used to accept Chatflow and stopped in some recent
        # version. Must classify as non-retriable so we don't burn 4x
        # retries on a permanent error.
        (
            'invoke app failed: request failed with status code: 400 '
            'and respond with: {"code":"invalid_param",'
            '"message":"unexpected app type","status":400}',
            ErrorCategory.INVALID_APP_TYPE,
        ),
        # A bare invalid_param without "unexpected app type" — e.g. an
        # unpublished app or inputs schema mismatch — should also be
        # non-retriable but not the more specific app-type bucket.
        (
            'invoke app failed: status 400 {"code":"invalid_param",'
            '"message":"something else","status":400}',
            ErrorCategory.INVALID_REQUEST,
        ),
    ],
)
def test_classify_invalid_param_variants_are_non_retriable(text: str, expected_category: ErrorCategory):
    verdict = classify(Exception(text))
    assert verdict.category == expected_category
    assert verdict.retriable is False, (
        "invalid_param errors are permanent — retrying just wastes the "
        "4-attempt budget and slows the workflow down"
    )


def test_unexpected_app_type_takes_precedence_over_invalid_param():
    """Both substrings appear in the same Dify error body. The classifier
    must surface the more specific category so the Web UI shows the
    app-type hint, not the generic invalid_request one."""
    msg = '{"code":"invalid_param","message":"unexpected app type"}'
    assert classify(Exception(msg)).category == ErrorCategory.INVALID_APP_TYPE


def test_classify_unknown_defaults_to_internal_retriable():
    """Unknown errors should retry — matches §7.1's "default → INTERNAL"."""
    verdict = classify(RuntimeError("something we have never seen"))
    assert verdict.category == ErrorCategory.INTERNAL
    assert verdict.retriable is True


def test_classify_case_insensitive():
    """Patterns are matched on lowercased text so case variations bucket."""
    verdict = classify(Exception("APP NOT FOUND"))
    assert verdict.category == ErrorCategory.INVALID_APP_ID


def test_classification_is_immutable():
    """Frozen dataclass — guards against accidental mutation downstream."""
    verdict: Classification = classify(Exception("timeout"))
    with pytest.raises((AttributeError, TypeError)):
        verdict.retriable = True  # type: ignore[misc]


def test_backend_client_error_message_preserved_in_classification():
    """The classifier must keep the original message so the run loop can
    persist a useful turn_results.error string."""
    verdict = classify(BackendClientError("custom: scenario not owned", status=404, body=""))
    assert "custom: scenario not owned" in verdict.message


def test_retry_backoff_schedule_is_monotonic():
    """Defensive: an out-of-order schedule would produce hump-shaped
    backoff and was an actual real bug in an earlier draft. Pin the order."""
    assert list(RETRY_BACKOFF_SECONDS) == sorted(RETRY_BACKOFF_SECONDS)
    assert all(s > 0 for s in RETRY_BACKOFF_SECONDS)


def test_status_takes_precedence_over_text():
    """When BackendClientError carries a status, status mapping wins
    over text-pattern matching — ensures we don't downgrade a 401 to
    INTERNAL just because the body contained 'timeout' or similar."""
    exc = BackendClientError("auth failed (timeout in body)", status=401, body="timeout")
    verdict = classify(exc)
    assert verdict.category == ErrorCategory.AUTH_FAILED
    assert verdict.retriable is False
