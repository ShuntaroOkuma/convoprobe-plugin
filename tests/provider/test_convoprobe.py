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


def test_validate_rejects_with_helpful_message_on_401(monkeypatch):
    err = BackendClientError("HTTP 401", status=401, body="invalid")
    fake = _FakeBackend(health_error=err)
    _patch(monkeypatch, fake)

    with pytest.raises(ToolProviderCredentialValidationError) as exc:
        ConvoProbeProvider()._validate_credentials({"convoprobe_api_token": TOKEN})
    assert "Re-issue" in str(exc.value)
    assert fake.closed is True


def test_validate_surfaces_generic_backend_failure(monkeypatch):
    err = BackendClientError("transport error", status=0, body="")
    fake = _FakeBackend(health_error=err)
    _patch(monkeypatch, fake)

    with pytest.raises(ToolProviderCredentialValidationError) as exc:
        ConvoProbeProvider()._validate_credentials({"convoprobe_api_token": TOKEN})
    assert "Could not reach" in str(exc.value)
    assert fake.closed is True


def test_validate_forwards_base_url(monkeypatch):
    fake = _FakeBackend()
    captured = _patch(monkeypatch, fake)

    ConvoProbeProvider()._validate_credentials({
        "convoprobe_api_token": TOKEN,
        "convoprobe_api_base_url": "https://self-host.example.com",
    })
    assert captured["token"] == TOKEN
    assert captured["base_url"] == "https://self-host.example.com"
