"""Unit tests for tools/run_scenario.RunScenarioTool.

The Tool itself is a thin shim that:
- forwards credentials + tool_parameters into helpers.run_executor
- yields a single JSON ToolInvokeMessage matching RunResult.to_payload()
- surfaces input validation + create_run failures as structured JSON
  messages rather than raising (Dify Studio shows yielded JSON inline
  in the workflow run panel; exceptions get wrapped in the noisy
  PluginInvokeError envelope)

scenario_id is currently a plain text-input (see tools/run_scenario.py
docstring for why dynamic-select was rolled back); _fetch_parameter_options
is therefore no longer wired and not covered here. helpers/backend_client
still exposes list_scenarios() so the dynamic-select path can be
restored when Dify ships the fix — the helper is covered in
tests/helpers/test_backend_client.py.
"""
from __future__ import annotations

from typing import Any

import pytest

from helpers.backend_client import BackendClientError
from tools.run_scenario import RunScenarioTool


TOKEN = "cp_" + "a" * 60


class _FakeBackend:
    """Stand-in for BackendClient that only needs to satisfy the bits
    RunScenarioTool reaches into (essentially: context manager + close).
    We do NOT subclass BackendClient because BackendClient.__init__
    validates the token and opens an httpx.Client; tests should not
    need to mimic either.
    """

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    # Context manager protocol mirrors helpers.backend_client.BackendClient
    # so production code can use `with BackendClient(...) as client:`.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _tool(token: str = TOKEN, base_url: str | None = None) -> RunScenarioTool:
    creds: dict[str, Any] = {"convoprobe_api_token": token}
    if base_url is not None:
        creds["convoprobe_api_base_url"] = base_url
    return RunScenarioTool.from_credentials(creds)


# --- _invoke ----------------------------------------------------------------
#
# T39.2: spike echo replaced with execute_scenario_run. We patch the
# helper at the import location (`tools.run_scenario.execute_scenario_run`)
# rather than reaching into helpers — the Tool's contract is "call the
# helper, yield the payload", so the test should pin that boundary.

from helpers.run_executor import RunResult


def _patch_executor(monkeypatch: pytest.MonkeyPatch, result_or_exc: Any) -> dict:
    """Replace execute_scenario_run with a controllable stub.

    Pass a RunResult to simulate a successful run, or an exception
    instance to simulate a failure (raised when the stub is called).
    Returns a captured-args dict so tests can assert what the Tool
    forwarded to the helper.
    """
    captured: dict[str, Any] = {}

    def _stub(session, client, *, app_id, scenario_id, **kwargs):
        captured["app_id"] = app_id
        captured["scenario_id"] = scenario_id
        captured["kwargs"] = kwargs
        if isinstance(result_or_exc, BaseException):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr("tools.run_scenario.execute_scenario_run", _stub)
    # Also patch BackendClient so the `with` block in _invoke doesn't try
    # to open a real httpx.Client — we don't care about it in these tests.
    monkeypatch.setattr("tools.run_scenario.BackendClient", lambda **kw: _FakeBackend())
    return captured


_HAPPY_RESULT = RunResult(
    run_id="run-1",
    scenario_id="scn-123",
    status="completed",
    completed_turns=3,
    estimated_turns=3,
    error_summary="",
    transcript_url="https://convoprobe.vercel.app/en/scenarios/executions/run-1",
)


def test_invoke_calls_executor_and_yields_payload(monkeypatch):
    """Happy path: helper returns a RunResult, Tool yields its
    to_payload() shape so downstream Dify nodes get a stable JSON."""
    captured = _patch_executor(monkeypatch, _HAPPY_RESULT)
    tool = _tool()

    messages = list(tool._invoke({
        "scenario_id": "scn-123",
        "target_app": {"app_id": "app-abc"},
    }))

    assert len(messages) == 1
    payload = messages[0].message.json_object
    assert payload == _HAPPY_RESULT.to_payload()
    assert captured["scenario_id"] == "scn-123"
    assert captured["app_id"] == "app-abc"


def test_invoke_extracts_target_app_from_raw_string(monkeypatch):
    """`app-selector` returns a dict from the dropdown but inside a
    workflow the slot can be fed by a variable, in which case Dify
    passes the raw app_id string. Both shapes must reach the helper
    with the same app_id."""
    captured = _patch_executor(monkeypatch, _HAPPY_RESULT)
    list(_tool()._invoke({
        "scenario_id": "scn-1",
        "target_app": "raw-string-id",
    }))
    assert captured["app_id"] == "raw-string-id"


def test_invoke_treats_whitespace_only_scenario_id_as_missing(monkeypatch):
    """Users will copy/paste the scenario UUID from the Web UI URL —
    accidentally pasting "  uuid  " or just whitespace should not slip
    through to a backend call. Trim first, then validate."""
    _patch_executor(monkeypatch, _HAPPY_RESULT)
    messages = list(_tool()._invoke({
        "scenario_id": "   ",
        "target_app": {"app_id": "app-1"},
    }))
    assert "error" in messages[0].message.json_object
    assert "scenario_id" in messages[0].message.json_object["error"]


def test_invoke_yields_error_for_missing_scenario_id(monkeypatch):
    """The dropdown should always be selected before run, but a workflow
    variable could leave it empty. The Tool yields a structured error
    rather than crashing or no-op'ing silently."""
    _patch_executor(monkeypatch, _HAPPY_RESULT)  # never called
    messages = list(_tool()._invoke({"scenario_id": "", "target_app": {"app_id": "app"}}))
    assert "error" in messages[0].message.json_object
    assert "scenario_id" in messages[0].message.json_object["error"]


def test_invoke_yields_error_for_missing_target_app(monkeypatch):
    _patch_executor(monkeypatch, _HAPPY_RESULT)
    messages = list(_tool()._invoke({"scenario_id": "scn-1"}))
    assert "error" in messages[0].message.json_object
    assert "target_app" in messages[0].message.json_object["error"]


def test_invoke_yields_error_for_missing_token(monkeypatch):
    _patch_executor(monkeypatch, _HAPPY_RESULT)
    # _tool() injects a token; build the Tool directly without one
    tool = RunScenarioTool.from_credentials({})
    messages = list(tool._invoke({
        "scenario_id": "scn-1",
        "target_app": {"app_id": "app-1"},
    }))
    assert "token" in messages[0].message.json_object.get("error", "").lower()


def test_invoke_yields_error_when_create_run_fails(monkeypatch):
    """create_run failures (auth/network/missing scenario) should surface
    as a structured JSON message, not as a raised exception that Dify
    Studio wraps in PluginInvokeError noise."""
    _patch_executor(
        monkeypatch,
        BackendClientError("HTTP 404", status=404, body=""),
    )
    messages = list(_tool()._invoke({
        "scenario_id": "scn-1",
        "target_app": {"app_id": "app-1"},
    }))
    payload = messages[0].message.json_object
    assert "error" in payload
    assert payload.get("status_code") == 404


def test_invoke_drops_unsupported_target_app_shapes(monkeypatch):
    """Anything that's neither a dict nor a string (None, list, int)
    should fall back to empty -> trip the missing-target_app error."""
    _patch_executor(monkeypatch, _HAPPY_RESULT)
    for bogus in (None, [], 42):
        messages = list(_tool()._invoke({
            "scenario_id": "scn-1",
            "target_app": bogus,
        }))
        payload = messages[0].message.json_object
        assert "error" in payload, f"expected fallback error for {bogus!r}, got {payload}"
