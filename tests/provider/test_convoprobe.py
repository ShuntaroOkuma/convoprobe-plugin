"""Unit tests for provider/convoprobe.ConvoProbeProvider (Spike T38).

Credential validation runs at install time inside Dify Studio; if it
raises ToolProviderCredentialValidationError the user sees an inline
error and the install fails fast. These tests cover the three branches
that determine whether that error fires:

- missing token -> always reject
- backend 401  -> reject with a "re-issue token" hint
- backend ok   -> accept
"""
from __future__ import annotations

from typing import Any

import pytest

from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from helpers.backend_client import BackendClientError
from provider.convoprobe import ConvoProbeProvider


TOKEN = "cp_" + "b" * 60


class _FakeBackend:
    def __init__(self, health_error: Exception | None = None):
        self._error = health_error
        self.closed = False

    def health(self):
        if self._error is not None:
            raise self._error
        return {"status": "ok"}

    def close(self):
        self.closed = True

    # Context manager protocol mirrors helpers.backend_client.BackendClient
    # so the production code (which uses `with BackendClient(...) as ...`)
    # exercises the same surface in tests.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _patch(monkeypatch: pytest.MonkeyPatch, fake: _FakeBackend) -> dict:
    captured: dict[str, Any] = {}

    def _factory(*args, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr("provider.convoprobe.BackendClient", _factory)
    return captured


def test_validate_accepts_when_health_succeeds(monkeypatch):
    fake = _FakeBackend()
    _patch(monkeypatch, fake)

    ConvoProbeProvider()._validate_credentials({"convoprobe_api_token": TOKEN})
    assert fake.closed is True


def test_validate_rejects_when_token_missing(monkeypatch):
    # Token absent means we should never reach the backend; install a
    # fake that would explode if invoked.
    _patch(monkeypatch, _FakeBackend(health_error=RuntimeError("must not call")))

    with pytest.raises(ToolProviderCredentialValidationError):
        ConvoProbeProvider()._validate_credentials({})


# Visual budget cap: Dify Studio's PluginInvokeError JSON wrapper eats
# ~80 visible characters before our message starts, and the line is
# truncated. We pin a soft limit so the action stays visible.
_MAX_MESSAGE_CHARS = 90


def test_validate_rejects_with_helpful_message_on_401(monkeypatch):
    err = BackendClientError("HTTP 401", status=401, body="invalid")
    fake = _FakeBackend(health_error=err)
    _patch(monkeypatch, fake)

    with pytest.raises(ToolProviderCredentialValidationError) as exc:
        ConvoProbeProvider()._validate_credentials({"convoprobe_api_token": TOKEN})
    msg = str(exc.value)
    # Action-first phrasing: must lead the user to issue a new token.
    assert msg.startswith("ConvoProbe token rejected")
    assert "Reissue" in msg
    # Avoid `>` so Dify Studio JSON wrapping doesn't escape to `>`.
    assert ">" not in msg
    # Stay under the visual budget so the action survives JSON wrap +
    # truncation in Dify Studio.
    assert len(msg) <= _MAX_MESSAGE_CHARS, f"message too long ({len(msg)} chars): {msg}"
    assert fake.closed is True


def test_validate_surfaces_generic_backend_failure(monkeypatch):
    err = BackendClientError("transport error", status=0, body="")
    fake = _FakeBackend(health_error=err)
    _patch(monkeypatch, fake)

    with pytest.raises(ToolProviderCredentialValidationError) as exc:
        ConvoProbeProvider()._validate_credentials({"convoprobe_api_token": TOKEN})
    msg = str(exc.value)
    assert msg.startswith("ConvoProbe Backend unreachable")
    assert "API Base URL" in msg
    assert ">" not in msg


def test_validate_missing_token_message_is_action_first(monkeypatch):
    # Same constraint as the 401 path: lead with the next step the user
    # should take, and avoid `>` to dodge JSON-escape ugliness.
    _patch(monkeypatch, _FakeBackend(health_error=RuntimeError("must not call")))

    with pytest.raises(ToolProviderCredentialValidationError) as exc:
        ConvoProbeProvider()._validate_credentials({})
    msg = str(exc.value)
    assert msg.startswith("ConvoProbe token required")
    assert ">" not in msg
    assert len(msg) <= _MAX_MESSAGE_CHARS, f"message too long ({len(msg)} chars): {msg}"


def test_validate_forwards_base_url(monkeypatch):
    fake = _FakeBackend()
    captured = _patch(monkeypatch, fake)

    ConvoProbeProvider()._validate_credentials({
        "convoprobe_api_token": TOKEN,
        "convoprobe_api_base_url": "https://self-host.example.com",
    })
    assert captured["token"] == TOKEN
    assert captured["base_url"] == "https://self-host.example.com"
