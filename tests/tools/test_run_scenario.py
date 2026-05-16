"""Unit tests for tools/run_scenario.RunScenarioTool (Spike T38).

The Tool itself is a thin shim; what we want to lock down is:
- the dynamic-select dropdown calls BackendClient.list_scenarios() and
  returns a list of ParameterOption (the SDK type Dify uses to populate
  the node config UI)
- missing/empty credentials short-circuit to an empty list so the node
  config UI never crashes on a bad token
- a 404 from the backend (spike phase, endpoint not yet wired) degrades
  to an empty list rather than bubbling up
- _invoke emits a single JSON message echoing the parameters; that
  message shape is what T39 will replace with the real run loop
"""
from __future__ import annotations

from typing import Any

import pytest

from dify_plugin.entities import ParameterOption

from helpers.backend_client import BackendClientError
from tools.run_scenario import RunScenarioTool


TOKEN = "cp_" + "a" * 60


class _FakeBackend:
    """Stand-in for BackendClient that only needs to satisfy the bits
    RunScenarioTool reaches into. We do NOT subclass BackendClient
    because BackendClient.__init__ validates the token and opens an
    httpx.Client; tests should not need to mimic either.
    """

    def __init__(self, scenarios: Any = None, error: Exception | None = None):
        self._scenarios = scenarios
        self._error = error
        self.closed = False

    def list_scenarios(self):
        if self._error is not None:
            raise self._error
        return self._scenarios

    def close(self):
        self.closed = True

    # Context manager protocol mirrors helpers.backend_client.BackendClient
    # so production code can use `with BackendClient(...) as client:`.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _patch_backend(monkeypatch: pytest.MonkeyPatch, fake: _FakeBackend) -> dict:
    """Replace BackendClient *as imported by run_scenario* so the Tool
    sees our fake. Capturing kwargs lets tests assert the credentials
    actually got forwarded.
    """
    captured: dict[str, Any] = {}

    def _factory(*args, **kwargs):
        captured.update(kwargs)
        return fake

    monkeypatch.setattr("tools.run_scenario.BackendClient", _factory)
    return captured


def _tool(token: str = TOKEN, base_url: str | None = None) -> RunScenarioTool:
    creds: dict[str, Any] = {"convoprobe_api_token": token}
    if base_url is not None:
        creds["convoprobe_api_base_url"] = base_url
    return RunScenarioTool.from_credentials(creds)


# --- _fetch_parameter_options ----------------------------------------------

def test_fetch_options_for_scenario_id_returns_parameter_options(monkeypatch):
    fake = _FakeBackend(scenarios=[
        {"id": "s1", "name": "FAQ"},
        {"id": "s2", "name": "Order intent"},
    ])
    _patch_backend(monkeypatch, fake)

    options = _tool()._fetch_parameter_options("scenario_id")

    assert len(options) == 2
    assert all(isinstance(o, ParameterOption) for o in options)
    assert [o.value for o in options] == ["s1", "s2"]
    assert options[0].label.en_US == "FAQ"
    # ja_JP is intentionally not passed: I18nObject in dify_plugin
    # only models en_US/zh_Hans/pt_BR and would silently drop ja_JP.
    assert fake.closed is True


def test_fetch_options_forwards_credentials_to_backend(monkeypatch):
    fake = _FakeBackend(scenarios=[])
    captured = _patch_backend(monkeypatch, fake)

    _tool(base_url="https://self-host.example.com")._fetch_parameter_options("scenario_id")

    assert captured["token"] == TOKEN
    assert captured["base_url"] == "https://self-host.example.com"


def test_fetch_options_returns_empty_for_unknown_parameter(monkeypatch):
    # We never want a non-scenario_id parameter to trigger a backend call;
    # patch with a fake that would explode if touched.
    _patch_backend(monkeypatch, _FakeBackend(error=RuntimeError("must not call")))

    assert _tool()._fetch_parameter_options("target_app") == []
    assert _tool()._fetch_parameter_options("wait_for_completion") == []


def test_fetch_options_empty_when_token_missing(monkeypatch):
    _patch_backend(monkeypatch, _FakeBackend(error=RuntimeError("must not call")))

    options = RunScenarioTool.from_credentials({})._fetch_parameter_options("scenario_id")
    assert options == []


def test_fetch_options_degrades_gracefully_on_backend_error(monkeypatch):
    """Spike phase: /scenarios may 404 because the Backend endpoint is
    not yet implemented. The node config UI must keep working."""
    fake = _FakeBackend(error=BackendClientError("HTTP 404", status=404, body=""))
    _patch_backend(monkeypatch, fake)

    assert _tool()._fetch_parameter_options("scenario_id") == []
    assert fake.closed is True


# --- _invoke ----------------------------------------------------------------

def test_invoke_yields_single_json_echo_message():
    """Spike contract: one JSON message with the chosen params. Replaced
    by real run-loop output in T39 — anchor the shape so the contract
    flip is intentional."""
    tool = _tool()
    messages = list(tool._invoke({
        "scenario_id": "scn-123",
        "target_app": {"app_id": "app-abc"},
        "wait_for_completion": False,
    }))

    assert len(messages) == 1
    payload = messages[0].message.json_object
    assert payload["spike"] is True
    assert payload["scenario_id"] == "scn-123"
    assert payload["target_app_id"] == "app-abc"
    assert payload["wait_for_completion"] is False


def test_invoke_defaults_when_optional_params_missing():
    tool = _tool()
    messages = list(tool._invoke({"scenario_id": "scn-1"}))
    payload = messages[0].message.json_object
    assert payload["target_app_id"] == ""
    assert payload["wait_for_completion"] is True  # default


def test_invoke_extracts_target_app_from_raw_string():
    """`app-selector` returns ``{app_id, app_type, ...}`` when the user
    picks from the dropdown, but inside a workflow the slot can also
    be fed by a variable or another node's output, in which case Dify
    passes the raw app_id string. Both shapes must yield the same
    target_app_id downstream."""
    tool = _tool()
    messages = list(tool._invoke({
        "scenario_id": "scn-1",
        "target_app": "raw-string-id",
    }))
    payload = messages[0].message.json_object
    assert payload["target_app_id"] == "raw-string-id"


def test_invoke_drops_unsupported_target_app_shapes():
    """Anything that's neither a dict nor a string (None, list, int)
    should fall back to empty rather than crash. The real run loop
    will reject the empty value when T39 wires it up."""
    tool = _tool()
    for bogus in (None, [], 42):
        messages = list(tool._invoke({
            "scenario_id": "scn-1",
            "target_app": bogus,
        }))
        payload = messages[0].message.json_object
        assert payload["target_app_id"] == "", f"unexpected non-empty for {bogus!r}: {payload}"
