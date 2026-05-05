"""ConvoProbe /run endpoint — production conversation loop (T20-T23).

Receives a trigger from Dify (Studio Test button, an HTTP node in a
workflow, or a curl from the user's own automation) carrying a
`scenario_id`. Acks immediately with 202 and runs the scenario in a
daemon thread:

  1. POST Backend /api/internal/plugin/runs to materialize the run +
     fetch the static step list (start node + default branches).
  2. For each step, call session.app.chat.invoke against the configured
     target_app, threading conversation_id across turns. Retry with
     exponential backoff for retriable errors per error_classifier.
  3. POST each turn's outcome to /runs/<id>/turns.
  4. POST /runs/<id>/complete with completed | partial | failed.

The endpoint never blocks the HTTP request beyond accept time; the loop
runs detached so a slow Dify chat does not pin the daemon worker that
fields the ack.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Mapping
from typing import Any

from werkzeug import Request, Response

from dify_plugin.interfaces.endpoint import Endpoint

from helpers.backend_client import BackendClient, BackendClientError, RunDescriptor
from helpers.error_classifier import RETRY_BACKOFF_SECONDS, classify

logger = logging.getLogger(__name__)


class RunEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        try:
            payload = r.get_json(force=True, silent=True) or {}
        except Exception:
            return _error(400, "invalid_json", "Request body must be valid JSON")

        scenario_id = payload.get("scenario_id")
        if not scenario_id:
            return _error(400, "missing_field", "scenario_id is required")

        token = settings.get("convoprobe_api_token")
        if not token:
            return _error(400, "missing_setting", "convoprobe_api_token is not configured")

        target_app = settings.get("target_app") or {}
        app_id = target_app.get("app_id") if isinstance(target_app, dict) else None
        if not app_id:
            return _error(400, "missing_setting", "target_app is not configured")

        base_url = settings.get("convoprobe_api_base_url") or None

        # Create the run synchronously so we can return the run_id (and
        # surface auth/scenario errors as 4xx instead of an opaque 202).
        # The client owns a persistent httpx.Client connection pool that
        # the daemon thread continues to reuse for record_turn/complete;
        # the thread's try/finally closes it.
        client = BackendClient(token=token, base_url=base_url)
        try:
            descriptor = client.create_run(scenario_id)
        except BackendClientError as e:
            client.close()
            return _error(
                _http_status_for(e.status),
                "create_run_failed",
                f"Backend rejected create_run: {e}",
            )

        threading.Thread(
            target=_run_scenario_async,
            args=(self.session, client, app_id, descriptor),
            daemon=True,
            name=f"convoprobe-run-{descriptor.run_id[:8]}",
        ).start()

        return Response(
            response=json.dumps({
                "run_id": descriptor.run_id,
                "scenario_id": descriptor.scenario_id,
                "status": "accepted",
                "estimated_turns": len(descriptor.steps),
            }),
            status=202,
            content_type="application/json",
        )


def _run_scenario_async(
    session: Any,
    client: BackendClient,
    app_id: str,
    descriptor: RunDescriptor,
) -> None:
    """Thin wrapper that guarantees the persistent httpx.Client is
    closed when the daemon thread exits, no matter how _run_scenario_loop
    terminates. Idempotent close — safe if the thread is interrupted late.
    """
    try:
        _run_scenario_loop(session, client, app_id, descriptor)
    finally:
        client.close()


def _run_scenario_loop(
    session: Any,
    client: BackendClient,
    app_id: str,
    descriptor: RunDescriptor,
) -> None:
    """Owns conversation_id continuity, retry, and final /complete
    reporting. Exceptions are caught and translated into a failed
    completion so a crash in here cannot leak a 'running' row.
    """
    completed_turns = 0
    final_status = "completed"
    error_summary = ""
    conversation_id = ""
    deadline = _deadline_from(descriptor)

    try:
        for step in descriptor.steps:
            if time.monotonic() >= deadline:
                final_status = "partial"
                error_summary = "total run timeout reached before all turns completed"
                break

            outcome = _run_one_turn(session, app_id, conversation_id, step)
            if outcome.conversation_id:
                conversation_id = outcome.conversation_id

            try:
                client.record_turn(
                    descriptor.run_id,
                    node_id=step["node_id"],
                    turn_number=step["turn_number"],
                    user_message=step.get("user_message", ""),
                    bot_response=outcome.bot_response,
                    conversation_id=outcome.conversation_id,
                    message_id=outcome.message_id,
                    response_time_ms=outcome.response_time_ms,
                    error=outcome.error,
                )
            except BackendClientError as e:
                logger.warning(
                    "convoprobe: record_turn failed; aborting run %s at turn %d: %s",
                    descriptor.run_id,
                    step["turn_number"],
                    e,
                )
                final_status = "failed"
                error_summary = f"record_turn failed: {e}"
                break

            if outcome.error:
                # Per architecture §7.1 turn-level errors abort the run
                # rather than continuing into a definitely-bad downstream
                # context.
                final_status = "failed"
                error_summary = f"turn {step['turn_number']} failed: {outcome.error}"
                break

            completed_turns = step["turn_number"]
    except Exception as e:  # safety net: never leave a 'running' row
        logger.exception("convoprobe: unexpected error in run loop %s", descriptor.run_id)
        final_status = "failed"
        error_summary = f"unexpected error: {e}"

    # Always attempt to mark the run terminal, even if we aborted early.
    try:
        client.complete_run(
            descriptor.run_id,
            status=final_status,
            completed_turns=completed_turns,
            error_summary=error_summary,
        )
    except BackendClientError as e:
        logger.error(
            "convoprobe: complete_run failed for %s; sweeper will reap: %s",
            descriptor.run_id,
            e,
        )


class _TurnOutcome:
    """Lightweight value object for per-turn results. Tuple/dataclass
    would also work; class lets us assign attrs incrementally inside
    _run_one_turn without allocating an intermediate dict.
    """

    __slots__ = ("bot_response", "conversation_id", "message_id", "response_time_ms", "error")

    def __init__(self) -> None:
        self.bot_response = ""
        self.conversation_id = ""
        self.message_id = ""
        self.response_time_ms = 0
        self.error = ""


def _run_one_turn(
    session: Any,
    app_id: str,
    conversation_id: str,
    step: dict[str, Any],
) -> _TurnOutcome:
    """Invoke Dify once with retry. Always returns; failures are stored
    in `outcome.error` so the caller can persist them as a turn record.
    """
    outcome = _TurnOutcome()
    user_message = step.get("user_message", "")
    last_error: BaseException | None = None

    # Attempt count = len(backoff) + 1 — the schedule lists *waits*, so a
    # 3-element list implies up to 4 attempts (initial + 3 retries).
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        started = time.monotonic()
        try:
            response = session.app.chat.invoke(
                app_id=app_id,
                query=user_message,
                inputs={},
                response_mode="blocking",
                conversation_id=conversation_id or None,
            )
            outcome.response_time_ms = int((time.monotonic() - started) * 1000)
            outcome.bot_response, outcome.conversation_id, outcome.message_id = _extract_dify_response(response)
            return outcome
        except Exception as e:  # noqa: BLE001 — SDK throws bare Exception
            last_error = e
            verdict = classify(e)
            if not verdict.retriable or attempt >= len(RETRY_BACKOFF_SECONDS):
                outcome.response_time_ms = int((time.monotonic() - started) * 1000)
                outcome.error = f"{verdict.category.value}: {verdict.message}"[:500]
                return outcome
            sleep_s = _jittered(RETRY_BACKOFF_SECONDS[attempt])
            logger.info(
                "convoprobe: retriable error (%s) on turn %d; sleeping %.2fs before retry %d",
                verdict.category.value,
                step.get("turn_number", -1),
                sleep_s,
                attempt + 1,
            )
            time.sleep(sleep_s)

    # Defensive: loop should always return inside, but guard anyway so a
    # future refactor can't silently drop a turn.
    outcome.error = f"INTERNAL: exhausted retries: {last_error}"[:500]
    return outcome


def _extract_dify_response(response: Any) -> tuple[str, str, str]:
    """Pull (answer, conversation_id, message_id) out of the SDK's
    inconsistent response shape — sometimes top-level, sometimes nested
    under "data". See verify.py for the keys observed in production.
    """
    if not isinstance(response, dict):
        return "", "", ""
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    answer = response.get("answer") or data.get("answer") or ""
    conv_id = response.get("conversation_id") or data.get("conversation_id") or ""
    msg_id = response.get("message_id") or data.get("message_id") or ""
    return answer, conv_id, msg_id


def _deadline_from(descriptor: RunDescriptor) -> float:
    """Absolute monotonic deadline for the whole run. Defaults to 600s
    (matches Backend's pluginRunTotalTimeoutSeconds) if the descriptor
    omits config — defensive only; Backend always populates it.
    """
    total = 600
    cfg = descriptor.config or {}
    raw = cfg.get("total_timeout_seconds")
    if isinstance(raw, int) and raw > 0:
        total = raw
    return time.monotonic() + total


def _jittered(base: float) -> float:
    """±20% jitter per architecture §7.2. random.uniform is fine — these
    are sleep schedules, not crypto."""
    return base * random.uniform(0.8, 1.2)


def _http_status_for(backend_status: int) -> int:
    """Surface 401/404 as-is (so the user sees the precise problem in
    Dify Studio's response panel); coalesce everything else to 502
    because from Dify's perspective the failure is "Backend unreachable".
    """
    if backend_status in (401, 403, 404, 422):
        return backend_status
    return 502


def _error(status: int, code: str, message: str) -> Response:
    return Response(
        response=json.dumps({"error": {"code": code, "message": message}}),
        status=status,
        content_type="application/json",
    )
