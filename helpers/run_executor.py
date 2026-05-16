"""End-to-end scenario run executor — shared by /run Endpoint and
RunScenarioTool.

The Plugin has two trigger surfaces (PRD v0.2 / ADR-008):
- `endpoints/run.py` — curl / HTTP-node trigger that came in with v0.1.
- `tools/run_scenario.py` — Dify workflow node trigger that landed with
  the v0.2 Tool plugin pivot.

Both trigger paths converge on the same synchronous run loop:

  1. Backend.create_run materializes a `scenario_executions` row and
     returns the static step list (start node + default branches).
  2. For each step, `session.app.chat.invoke` runs the user query
     against the configured target_app, threading `conversation_id`
     across turns so the chatbot keeps multi-turn context.
  3. Each turn outcome is posted to Backend via record_turn.
  4. complete_run flips the run terminal (completed / failed / partial).

Why synchronous (revised from Day 3 ADR-003)
--------------------------------------------
We originally spawned a daemon thread and returned 202 so the inbound
request would not block. Phase 2 verification on Dify Cloud confirmed
this does not work: `session.app.chat.invoke` (Reverse Invocation)
relies on a TCP channel tied to the inbound request lifecycle, and the
channel is closed the moment we return. Background threads therefore
cannot reach Dify, and the run hangs in `running` until the Backend
sweeper reaps it.

Synchronous mode side-steps this entirely. Trade-off: the Plugin
worker is held for the duration of the run (~5-15s/turn x N turns).
For MVP scenarios (3-5 turns) this stays inside dify_plugin's
MAX_REQUEST_TIMEOUT_S=120s budget. Longer scenarios will hit that
limit; if the request is killed mid-run, the Backend sweeper still
fails the run after pluginRunTotalTimeoutSeconds (10 min).
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .backend_client import BackendClient, BackendClientError, RunDescriptor
from .error_classifier import RETRY_BACKOFF_SECONDS, classify

logger = logging.getLogger(__name__)

# WebUI base used to construct transcript_url. Hardcoded for MVP — when
# self-hosted ConvoProbe deployments come up, expose this as a Plugin
# credential alongside `convoprobe_api_base_url`.
DEFAULT_WEB_UI_BASE_URL = "https://convoprobe.vercel.app"


@dataclass(frozen=True)
class RunResult:
    """Terminal state of a single scenario run.

    Frozen so the response payload cannot be mutated after the loop
    settles; the Tool/Endpoint just yields it as JSON.
    """

    run_id: str
    scenario_id: str
    status: str          # "completed" | "failed" | "partial"
    completed_turns: int
    estimated_turns: int
    error_summary: str
    transcript_url: str

    def to_payload(self) -> dict[str, Any]:
        """Stable JSON shape returned by both /run and RunScenarioTool."""
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "status": self.status,
            "completed_turns": self.completed_turns,
            "estimated_turns": self.estimated_turns,
            "error_summary": self.error_summary,
            "transcript_url": self.transcript_url,
        }


def execute_scenario_run(
    session: Any,
    client: BackendClient,
    *,
    app_id: str,
    scenario_id: str,
    web_ui_base_url: str = DEFAULT_WEB_UI_BASE_URL,
    locale: str = "en",
) -> RunResult:
    """Run a scenario end-to-end and return the terminal RunResult.

    Raises BackendClientError on create_run failure (auth / scenario
    not found / network) — the caller decides how to surface that
    (HTTP 4xx for the Endpoint, error message for the Tool). Once the
    descriptor is fetched, the function never raises: per-turn errors
    are recorded and the run is closed with status='failed' or
    'partial'.
    """
    descriptor = client.create_run(scenario_id)
    inner = _run_scenario_sync(session, client, app_id, descriptor)
    return RunResult(
        run_id=descriptor.run_id,
        scenario_id=descriptor.scenario_id,
        status=inner["status"],
        completed_turns=inner["completed_turns"],
        estimated_turns=len(descriptor.steps),
        error_summary=inner.get("error_summary", ""),
        transcript_url=_build_transcript_url(
            web_ui_base_url, locale, descriptor.scenario_id, descriptor.run_id,
        ),
    )


def _build_transcript_url(base: str, locale: str, scenario_id: str, run_id: str) -> str:
    """Frontend route: /<locale>/scenarios/executions/<execution_id>.
    scenario_id is currently unused but kept in the signature so a
    future redesign (e.g., nested under scenario detail) doesn't need
    to thread the value through fresh.

    `locale` defaults to "en" at the call site because the Dify Plugin
    SDK's ToolRuntime only exposes credentials + user_id today (see
    dify_plugin/entities/tool.py:34 — no `locale` or `user_lang`
    field). ConvoProbe Web UI auto-redirects to the user's browser
    locale on first visit, so an English URL is a benign default. When
    the SDK adds a locale getter the call site in
    tools/run_scenario.py can pass it through without changing this
    builder.
    """
    _ = scenario_id
    return f"{base.rstrip('/')}/{locale}/scenarios/executions/{run_id}"


# --- internal: per-turn execution ------------------------------------------

class _TurnOutcome:
    """Lightweight per-turn record. Not a dataclass because we want to
    assign attrs incrementally inside _run_one_turn without round-tripping
    through a constructor on each retry attempt.
    """

    __slots__ = ("bot_response", "conversation_id", "message_id", "response_time_ms", "error")

    def __init__(self) -> None:
        self.bot_response = ""
        self.conversation_id = ""
        self.message_id = ""
        self.response_time_ms = 0
        self.error = ""


def _run_scenario_sync(
    session: Any,
    client: BackendClient,
    app_id: str,
    descriptor: RunDescriptor,
) -> dict[str, Any]:
    """Loop body. Owns conversation_id continuity, retry, and the final
    /complete reporting. Catches all exceptions so a crash in here cannot
    leak a 'running' row in the Backend.
    """
    # Status accumulators live OUTSIDE the try so the complete_run leg
    # below can see them even if initialization (e.g., _deadline_from)
    # blew up. Anything that *can* raise is inside the try.
    completed_turns = 0
    final_status = "completed"
    error_summary = ""

    try:
        conversation_id = ""
        deadline = _deadline_from(descriptor)

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
    except Exception as e:  # noqa: BLE001 — safety net, never leak a 'running' row
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
        # Surface the failure via the response body. Backend sweeper is
        # the safety net for the row state.
        if not error_summary:
            error_summary = f"complete_run failed: {e}"
        if final_status == "completed":
            final_status = "failed"

    return {
        "status": final_status,
        "completed_turns": completed_turns,
        "error_summary": error_summary,
    }


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
    # Forward-compat: when the Backend's PluginRunStep gains an `inputs`
    # field for chatflows that require structured input variables, we
    # pick it up here without a Plugin code change. `or {}` covers both
    # missing key and explicit JSON null.
    step_inputs = step.get("inputs") or {}
    last_error: BaseException | None = None

    # `started` lives outside the retry loop so response_time_ms reflects
    # the WALL-CLOCK turn cost (all attempts + jittered backoff sleeps),
    # not just the final attempt's invoke duration. Users debugging slow
    # turns want to see "this turn took 18s including 2 retries", not
    # "the lucky 3rd attempt took 4s".
    started = time.monotonic()

    # Attempt count = len(backoff) + 1 — the schedule lists *waits*, so a
    # 3-element list implies up to 4 attempts (initial + 3 retries).
    for attempt in range(len(RETRY_BACKOFF_SECONDS) + 1):
        try:
            response = session.app.chat.invoke(
                app_id=app_id,
                query=user_message,
                inputs=step_inputs,
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
    outcome.response_time_ms = int((time.monotonic() - started) * 1000)
    outcome.error = f"INTERNAL: exhausted retries: {last_error}"[:500]
    return outcome


def _extract_dify_response(response: Any) -> tuple[str, str, str]:
    """Pull (answer, conversation_id, message_id) out of the SDK's
    inconsistent response shape — sometimes top-level, sometimes nested
    under "data". See endpoints/verify.py for the keys observed in
    production.
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
