"""HTTP client to ConvoProbe Backend's Plugin-internal API.

Wraps the three POST endpoints under `/api/internal/plugin/*` plus a health
check. Authentication is the long-lived `cp_<token>` (ADR-004) sent as
`Authorization: Bearer <token>`.

Network errors and non-2xx responses are surfaced as `BackendClientError`
with enough context (status, body excerpt) for the run loop to decide
whether to retry or abort. The client itself does not retry; the caller
(error_classifier + run loop) owns that policy so the policy lives in one
place.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://convoprobe-production.up.railway.app"

DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0


class BackendClientError(Exception):
    """Raised for any failure talking to ConvoProbe Backend.

    Attributes:
        status: HTTP status code, or 0 if the request never reached the
            server (DNS, connect, TLS errors).
        body: Truncated response body for debugging. Never logged in
            full to avoid leaking PII per architecture §7.4.
    """

    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass(frozen=True)
class RunDescriptor:
    """Mirror of Backend's domain.PluginRunDescriptor.

    Frozen so a turn-loop bug cannot mutate the step list out from under
    a concurrent retry.
    """

    run_id: str
    scenario_id: str
    max_turns: int
    steps: list[dict[str, Any]]
    config: dict[str, Any]


class BackendClient:
    """Thin synchronous HTTP client. Methods correspond 1:1 to the
    Plugin-internal endpoints in plugin_internal_handlers.go.

    Holds a persistent ``httpx.Client`` so a single run (1 create_run +
    N record_turn + 1 complete_run) reuses one TCP/TLS connection
    instead of paying handshake cost per call. Callers should ``close()``
    when done; the class is also a context manager. ``httpx.Client``
    itself is thread-safe per upstream docs, which matches the run
    loop pattern (descriptor fetched on the request thread, turns
    posted from the daemon thread).
    """

    def __init__(
        self,
        token: str,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        *,
        _client: httpx.Client | None = None,
    ):
        if not token:
            raise ValueError("token is required")
        self._token = token
        self._base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # _client is intentionally underscore-prefixed: production callers
        # should let us own the lifecycle. Tests inject one with a
        # MockTransport for in-memory routing.
        self._client = _client if _client is not None else httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        """Release the underlying connection pool. Safe to call multiple
        times; httpx.Client.close() is idempotent."""
        self._client.close()

    def __enter__(self) -> "BackendClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- public surface ----------------------------------------------------

    def health(self) -> dict[str, Any]:
        """GET /api/internal/plugin/health — confirms token is valid."""
        return self._request("GET", "/api/internal/plugin/health")

    def list_scenarios(self) -> list[dict[str, Any]]:
        """GET /api/internal/plugin/scenarios — list scenarios visible to
        this token, for the Tool plugin's dynamic dropdown (PRD v0.2 F4).

        Returns a list of ``{"id": str, "name": str}`` dicts. The Backend
        endpoint is not yet implemented at the time of this spike (T38);
        callers should treat a 404 as "feature not yet wired" and degrade
        gracefully rather than failing the whole tool config UI.
        """
        body = self._request("GET", "/api/internal/plugin/scenarios")
        items = body.get("scenarios") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return []
        result: list[dict[str, Any]] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            sid = it.get("id")
            name = it.get("name") or sid
            if isinstance(sid, str) and sid:
                result.append({"id": sid, "name": str(name)})
        return result

    def create_run(self, scenario_id: str) -> RunDescriptor:
        """POST /api/internal/plugin/runs.

        Returns the descriptor the run loop will iterate over. Raises
        BackendClientError on 4xx (e.g., scenario not owned by token).
        """
        body = self._request(
            "POST",
            "/api/internal/plugin/runs",
            json_body={"scenario_id": scenario_id},
        )
        try:
            return RunDescriptor(
                run_id=body["run_id"],
                scenario_id=body["scenario_id"],
                max_turns=body["max_turns"],
                steps=body["steps"],
                config=body["config"],
            )
        except (KeyError, TypeError) as e:
            raise BackendClientError(
                f"create_run: unexpected response shape: {e}",
                status=200,
                body=_truncate(body),
            ) from e

    def record_turn(
        self,
        run_id: str,
        *,
        node_id: str,
        turn_number: int,
        user_message: str,
        bot_response: str,
        conversation_id: str = "",
        message_id: str = "",
        response_time_ms: int = 0,
        error: str = "",
    ) -> None:
        """POST /api/internal/plugin/runs/<run_id>/turns.

        Empty `error` means the turn succeeded. Non-empty marks the turn
        failed in the database; the run can still continue or be ended
        with /complete.
        """
        self._request(
            "POST",
            f"/api/internal/plugin/runs/{run_id}/turns",
            json_body={
                "node_id": node_id,
                "turn_number": turn_number,
                "user_message": user_message,
                "bot_response": bot_response,
                "conversation_id": conversation_id,
                "message_id": message_id,
                "response_time_ms": response_time_ms,
                "error": error,
            },
        )

    def complete_run(
        self,
        run_id: str,
        *,
        status: str,
        completed_turns: int,
        error_summary: str = "",
    ) -> None:
        """POST /api/internal/plugin/runs/<run_id>/complete.

        `status` must be one of "completed" | "failed" | "partial"; the
        Backend rejects others with 400.
        """
        self._request(
            "POST",
            f"/api/internal/plugin/runs/{run_id}/complete",
            json_body={
                "status": status,
                "completed_turns": completed_turns,
                "error_summary": error_summary,
            },
        )

    # --- internals ---------------------------------------------------------

    def _request(self, method: str, path: str, *, json_body: Any = None) -> Any:
        url = self._base + path
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            resp = self._client.request(
                method,
                url,
                headers=headers,
                json=json_body,
            )
        except httpx.HTTPError as e:
            raise BackendClientError(
                f"{method} {path}: transport error: {e}",
                status=0,
                body="",
            ) from e

        if resp.status_code >= 400:
            raise BackendClientError(
                f"{method} {path}: HTTP {resp.status_code}",
                status=resp.status_code,
                body=_truncate(resp.text),
            )

        if not resp.content:
            return {}
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise BackendClientError(
                f"{method} {path}: invalid JSON response: {e}",
                status=resp.status_code,
                body=_truncate(resp.text),
            ) from e


def _truncate(value: Any, limit: int = 500) -> str:
    """Stringify + truncate response bodies before exposing them in
    exceptions / logs. Bot answers and PII may be in scope per
    architecture §7.4, so we cap at 500 chars rather than dumping
    unbounded payloads.
    """
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            value = str(value)
    if len(value) > limit:
        return value[:limit] + "...[truncated]"
    return value
