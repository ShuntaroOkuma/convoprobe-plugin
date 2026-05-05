"""Unit tests for helpers/backend_client.BackendClient.

Uses httpx.MockTransport for all HTTP calls — no network. The
production client constructs a fresh request per call (no Client
instance), so we monkeypatch httpx.request to route through our
transport. This keeps the production code idiomatic (one-shot
requests, no client lifecycle to manage) while still making it
fully testable.
"""
from __future__ import annotations

import json
from typing import Callable

import httpx
import pytest

from helpers.backend_client import (
    DEFAULT_BASE_URL,
    BackendClient,
    BackendClientError,
    RunDescriptor,
)

# --- helpers ----------------------------------------------------------------

TOKEN = "cp_aabbccdd11223344556677889900aabbccdd11223344556677889900aabbcc"


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    """Replace httpx.request with a transport-backed shim. Returns a
    captured list of every request the test caused so assertions can
    inspect headers, bodies, and URLs.
    """
    captured: list[httpx.Request] = []

    def _route(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    transport = httpx.MockTransport(_route)

    def _request(method: str, url: str, **kwargs) -> httpx.Response:
        # MockTransport requires we build the request through a Client
        # so it picks up the transport. Cheap to instantiate per call.
        with httpx.Client(transport=transport) as client:
            return client.request(method, url, **kwargs)

    monkeypatch.setattr("helpers.backend_client.httpx.request", _request)
    return captured


# --- constructor ------------------------------------------------------------

def test_constructor_rejects_empty_token():
    with pytest.raises(ValueError):
        BackendClient(token="")


def test_constructor_strips_trailing_slash_from_base_url():
    c = BackendClient(token=TOKEN, base_url="https://example.com/")
    assert c._base == "https://example.com"  # noqa: SLF001


def test_constructor_defaults_base_url():
    c = BackendClient(token=TOKEN)
    assert c._base == DEFAULT_BASE_URL.rstrip("/")  # noqa: SLF001


# --- health -----------------------------------------------------------------

def test_health_sends_bearer_and_returns_payload(monkeypatch: pytest.MonkeyPatch):
    captured = _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "ok", "checked_at": "2026-05-05T00:00:00Z"}),
    )
    client = BackendClient(token=TOKEN, base_url="https://api.example.com")
    body = client.health()

    assert body["status"] == "ok"
    assert len(captured) == 1
    req = captured[0]
    assert req.method == "GET"
    assert str(req.url) == "https://api.example.com/api/internal/plugin/health"
    assert req.headers.get("authorization") == f"Bearer {TOKEN}"


# --- create_run -------------------------------------------------------------

def test_create_run_happy_path(monkeypatch: pytest.MonkeyPatch):
    payload = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "scenario_id": "22222222-2222-2222-2222-222222222222",
        "max_turns": 3,
        "steps": [
            {"node_id": "44444444-4444-4444-4444-444444444444", "turn_number": 1, "user_message": "hi"},
        ],
        "config": {"max_turn_seconds": 60, "total_timeout_seconds": 600},
    }
    captured = _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(201, json=payload),
    )

    client = BackendClient(token=TOKEN, base_url="https://api.example.com")
    descriptor = client.create_run("22222222-2222-2222-2222-222222222222")

    assert isinstance(descriptor, RunDescriptor)
    assert descriptor.run_id == "11111111-1111-1111-1111-111111111111"
    assert descriptor.max_turns == 3
    assert descriptor.config["total_timeout_seconds"] == 600

    req = captured[0]
    assert req.method == "POST"
    assert str(req.url).endswith("/api/internal/plugin/runs")
    body = json.loads(req.content)
    assert body == {"scenario_id": "22222222-2222-2222-2222-222222222222"}


def test_create_run_descriptor_is_frozen(monkeypatch: pytest.MonkeyPatch):
    """A frozen dataclass would prevent the run loop from corrupting
    the step list mid-iteration. Pin the immutability."""
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(
            201,
            json={
                "run_id": "11111111-1111-1111-1111-111111111111",
                "scenario_id": "22222222-2222-2222-2222-222222222222",
                "max_turns": 1,
                "steps": [],
                "config": {},
            },
        ),
    )
    descriptor = BackendClient(token=TOKEN).create_run("22222222-2222-2222-2222-222222222222")
    with pytest.raises((AttributeError, TypeError)):
        descriptor.run_id = "other"  # type: ignore[misc]


def test_create_run_raises_on_missing_keys(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(201, json={"run_id": "x", "scenario_id": "y"}),  # missing max_turns/steps/config
    )
    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).create_run("22222222-2222-2222-2222-222222222222")
    assert "unexpected response shape" in str(exc_info.value)


def test_create_run_4xx_propagates_status(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(404, json={"error": "scenario not found"}),
    )
    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).create_run("22222222-2222-2222-2222-222222222222")
    assert exc_info.value.status == 404


# --- record_turn ------------------------------------------------------------

def test_record_turn_serializes_full_payload(monkeypatch: pytest.MonkeyPatch):
    captured = _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "recorded", "next_step": None}),
    )

    client = BackendClient(token=TOKEN, base_url="https://api.example.com")
    client.record_turn(
        "11111111-1111-1111-1111-111111111111",
        node_id="44444444-4444-4444-4444-444444444444",
        turn_number=1,
        user_message="hello",
        bot_response="hi there",
        conversation_id="conv_xyz",
        message_id="msg_xyz",
        response_time_ms=4521,
        error="",
    )

    req = captured[0]
    assert str(req.url).endswith("/api/internal/plugin/runs/11111111-1111-1111-1111-111111111111/turns")
    body = json.loads(req.content)
    assert body["node_id"] == "44444444-4444-4444-4444-444444444444"
    assert body["turn_number"] == 1
    assert body["bot_response"] == "hi there"
    assert body["response_time_ms"] == 4521
    assert body["error"] == ""


def test_record_turn_propagates_409_conflict(monkeypatch: pytest.MonkeyPatch):
    """409 means the run finished while we were processing a turn — the
    run loop must see this distinct from a generic 4xx so it can stop
    posting further turns and skip the /complete call."""
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(409, json={"error": "run is already finished"}),
    )
    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).record_turn(
            "11111111-1111-1111-1111-111111111111",
            node_id="44444444-4444-4444-4444-444444444444",
            turn_number=1,
            user_message="x",
            bot_response="y",
        )
    assert exc_info.value.status == 409


# --- complete_run -----------------------------------------------------------

def test_complete_run_serializes_status(monkeypatch: pytest.MonkeyPatch):
    captured = _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, json={"status": "ok"}),
    )
    BackendClient(token=TOKEN).complete_run(
        "11111111-1111-1111-1111-111111111111",
        status="completed",
        completed_turns=3,
        error_summary="",
    )
    body = json.loads(captured[0].content)
    assert body == {"status": "completed", "completed_turns": 3, "error_summary": ""}


# --- transport-level errors -------------------------------------------------

def test_transport_error_yields_status_zero(monkeypatch: pytest.MonkeyPatch):
    """DNS / connect / TLS failures get status=0 so the classifier
    routes them to BACKEND_UNREACHABLE."""

    def _raise(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed")

    _patch_httpx(monkeypatch, _raise)

    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).health()
    assert exc_info.value.status == 0


def test_invalid_json_response_raises(monkeypatch: pytest.MonkeyPatch):
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(200, content=b"<html>not json</html>"),
    )
    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).health()
    # Body should be present (truncated) so debug logs can show what came back.
    assert "not json" in exc_info.value.body


def test_truncates_long_response_body_in_error(monkeypatch: pytest.MonkeyPatch):
    """500-char cap from architecture §7.4 — guard against a Backend
    bug that returns a 1MB error page from leaking into our exception."""
    huge = "X" * 5000
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(500, content=huge.encode()),
    )
    with pytest.raises(BackendClientError) as exc_info:
        BackendClient(token=TOKEN).health()
    assert len(exc_info.value.body) < 600  # 500 + "...[truncated]" overhead
    assert exc_info.value.body.endswith("...[truncated]")


def test_204_empty_body_does_not_crash(monkeypatch: pytest.MonkeyPatch):
    """An endpoint that legitimately returns no body should not crash
    the JSON parser."""
    _patch_httpx(
        monkeypatch,
        lambda r: httpx.Response(204),
    )
    # Use record_turn since it expects no return value.
    BackendClient(token=TOKEN).record_turn(
        "11111111-1111-1111-1111-111111111111",
        node_id="44444444-4444-4444-4444-444444444444",
        turn_number=1,
        user_message="x",
        bot_response="y",
    )
