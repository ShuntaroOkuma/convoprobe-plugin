"""Unit tests for helpers/run_executor — the shared sync run loop.

The loop itself is what /run Endpoint and RunScenarioTool both consume,
so the contract pinned here is what BOTH triggers will get. Mock the
session (Reverse Invocation) and BackendClient surfaces so the tests
can drive the loop deterministically without hitting Dify or the
ConvoProbe Backend.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from helpers.backend_client import BackendClientError, RunDescriptor
from helpers.run_executor import (
    DEFAULT_WEB_UI_BASE_URL,
    RunResult,
    _build_transcript_url,
    execute_scenario_run,
)


# --- helpers ---------------------------------------------------------------

def _descriptor(run_id: str = "run-1", scenario_id: str = "scn-1", steps: list[dict] | None = None) -> RunDescriptor:
    """Minimal descriptor the run loop can iterate over."""
    return RunDescriptor(
        run_id=run_id,
        scenario_id=scenario_id,
        max_turns=len(steps) if steps else 0,
        steps=steps or [],
        config={"max_turn_seconds": 60, "total_timeout_seconds": 600},
    )


def _step(turn: int, msg: str) -> dict[str, Any]:
    return {
        "node_id": f"node-{turn}",
        "turn_number": turn,
        "user_message": msg,
    }


class _FakeClient:
    """Just enough BackendClient surface for the loop. Recording everything
    so assertions can inspect what the loop actually sent to the Backend.
    """

    def __init__(
        self,
        descriptor: RunDescriptor,
        record_turn_error: Exception | None = None,
        complete_run_error: Exception | None = None,
    ):
        self.descriptor = descriptor
        self.record_turn_error = record_turn_error
        self.complete_run_error = complete_run_error
        self.create_calls: list[str] = []
        self.recorded_turns: list[dict[str, Any]] = []
        self.complete_calls: list[dict[str, Any]] = []

    def create_run(self, scenario_id: str) -> RunDescriptor:
        self.create_calls.append(scenario_id)
        return self.descriptor

    def record_turn(self, run_id: str, **kwargs):
        if self.record_turn_error is not None:
            raise self.record_turn_error
        self.recorded_turns.append({"run_id": run_id, **kwargs})

    def complete_run(self, run_id: str, **kwargs):
        if self.complete_run_error is not None:
            raise self.complete_run_error
        self.complete_calls.append({"run_id": run_id, **kwargs})


def _fake_session(responses: list[dict[str, Any]] | Exception) -> MagicMock:
    """Build a session whose `session.app.chat.invoke(...)` cycles through
    the supplied responses (one per call). Pass an Exception instance to
    have every call raise it (useful for retry-budget tests).
    """
    session = MagicMock()
    if isinstance(responses, BaseException):
        session.app.chat.invoke.side_effect = responses
    else:
        session.app.chat.invoke.side_effect = list(responses)
    return session


# --- _build_transcript_url -------------------------------------------------

def test_transcript_url_builds_from_default_base():
    url = _build_transcript_url(DEFAULT_WEB_UI_BASE_URL, "en", "scn-x", "run-y")
    assert url == "https://convoprobe.vercel.app/en/scenarios/executions/run-y"


def test_transcript_url_strips_trailing_slash_from_base():
    url = _build_transcript_url("https://example.com/", "ja", "scn", "run-1")
    assert url == "https://example.com/ja/scenarios/executions/run-1"


# --- execute_scenario_run --------------------------------------------------

def test_execute_run_happy_path_completes_all_turns():
    desc = _descriptor(steps=[_step(1, "hello"), _step(2, "follow up")])
    session = _fake_session([
        {"answer": "hi", "conversation_id": "conv-1", "message_id": "msg-1"},
        {"answer": "yes", "conversation_id": "conv-1", "message_id": "msg-2"},
    ])
    client = _FakeClient(desc)

    result = execute_scenario_run(session, client, app_id="app-x", scenario_id="scn-1")

    assert isinstance(result, RunResult)
    assert result.status == "completed"
    assert result.completed_turns == 2
    assert result.estimated_turns == 2
    assert result.error_summary == ""
    assert result.run_id == "run-1"
    assert result.transcript_url.endswith("/en/scenarios/executions/run-1")

    # Backend was driven the way the contract claims.
    assert client.create_calls == ["scn-1"]
    assert len(client.recorded_turns) == 2
    assert client.recorded_turns[0]["bot_response"] == "hi"
    assert client.recorded_turns[0]["conversation_id"] == "conv-1"
    assert len(client.complete_calls) == 1
    assert client.complete_calls[0]["status"] == "completed"
    assert client.complete_calls[0]["completed_turns"] == 2


def test_execute_run_threads_conversation_id_across_turns():
    """conversation_id from turn N flows into the invoke call of turn N+1
    so the chatbot keeps multi-turn context. This is the core value of
    the synchronous loop — pin it explicitly."""
    desc = _descriptor(steps=[_step(1, "q1"), _step(2, "q2")])
    session = _fake_session([
        {"answer": "a1", "conversation_id": "conv-thread", "message_id": "m1"},
        {"answer": "a2", "conversation_id": "conv-thread", "message_id": "m2"},
    ])
    execute_scenario_run(session, _FakeClient(desc), app_id="app", scenario_id="scn-1")

    calls = session.app.chat.invoke.call_args_list
    assert calls[0].kwargs["conversation_id"] is None
    assert calls[1].kwargs["conversation_id"] == "conv-thread"


def test_execute_run_marks_failed_when_record_turn_breaks(monkeypatch):
    """If record_turn dies mid-loop the run is closed as failed, with
    an error_summary that names the failure. The remaining turns are
    not invoked."""
    desc = _descriptor(steps=[_step(1, "q1"), _step(2, "q2")])
    session = _fake_session([
        {"answer": "a1", "conversation_id": "conv-1", "message_id": "m1"},
    ])
    client = _FakeClient(
        desc,
        record_turn_error=BackendClientError("HTTP 500", status=500, body=""),
    )

    result = execute_scenario_run(session, client, app_id="app", scenario_id="scn-1")

    assert result.status == "failed"
    assert "record_turn failed" in result.error_summary
    # Loop stopped — second turn never invoked.
    assert session.app.chat.invoke.call_count == 1
    # complete_run still ran so the row doesn't leak as 'running'.
    assert len(client.complete_calls) == 1
    assert client.complete_calls[0]["status"] == "failed"


def test_execute_run_marks_failed_on_turn_level_invoke_error(monkeypatch):
    """A Dify invoke error that exhausts retries should mark the turn
    as failed and abort the run rather than continuing into a bad
    context (per architecture §7.1)."""
    monkeypatch.setattr("helpers.run_executor.RETRY_BACKOFF_SECONDS", ())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    desc = _descriptor(steps=[_step(1, "q1"), _step(2, "q2")])
    session = _fake_session(Exception("permission denied for app"))
    client = _FakeClient(desc)

    result = execute_scenario_run(session, client, app_id="app", scenario_id="scn-1")

    assert result.status == "failed"
    assert "turn 1 failed" in result.error_summary
    assert "PERMISSION_DENIED" in result.error_summary
    # Turn 1 was recorded with the error, turn 2 was not invoked.
    assert len(client.recorded_turns) == 1
    assert client.recorded_turns[0]["error"].startswith("PERMISSION_DENIED")
    assert session.app.chat.invoke.call_count == 1


def test_execute_run_propagates_create_run_failure():
    """create_run errors are the caller's signal to surface a 4xx —
    don't swallow them like per-turn errors."""
    client = _FakeClient(_descriptor())
    client.create_run = lambda scenario_id: (_ for _ in ()).throw(  # noqa: B023
        BackendClientError("HTTP 404", status=404, body=""),
    )
    with pytest.raises(BackendClientError) as exc:
        execute_scenario_run(MagicMock(), client, app_id="app", scenario_id="missing")
    assert exc.value.status == 404


def test_execute_run_complete_run_failure_flips_status_to_failed():
    """If we managed to run all turns but complete_run errored, the
    sweeper still owns the row state. Surface 'failed' to the caller
    so the workflow downstream doesn't act on stale 'completed'."""
    desc = _descriptor(steps=[_step(1, "q1")])
    session = _fake_session([
        {"answer": "a1", "conversation_id": "c", "message_id": "m"},
    ])
    client = _FakeClient(
        desc,
        complete_run_error=BackendClientError("HTTP 500", status=500, body=""),
    )

    result = execute_scenario_run(session, client, app_id="app", scenario_id="scn-1")

    assert result.status == "failed"
    assert "complete_run failed" in result.error_summary


def test_run_result_to_payload_is_stable_json_shape():
    """Tool and Endpoint both yield this exact shape downstream. Pin the
    field set so a future refactor can't quietly add/remove keys."""
    r = RunResult(
        run_id="r", scenario_id="s", status="completed",
        completed_turns=2, estimated_turns=3, error_summary="",
        transcript_url="https://x/y",
    )
    payload = r.to_payload()
    assert set(payload.keys()) == {
        "run_id", "scenario_id", "status",
        "completed_turns", "estimated_turns",
        "error_summary", "transcript_url",
    }
