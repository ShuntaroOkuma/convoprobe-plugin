"""ConvoProbe /run endpoint — synchronous conversation loop (T20-T23).

Receives a trigger from outside Dify (curl, HTTP-node, CI/CD tools)
carrying a `scenario_id` and runs the scenario inline within the
inbound request, returning the terminal result. The run loop itself
lives in `helpers.run_executor` so the new RunScenarioTool (PRD v0.2
Tool plugin) shares the same execution path — see ADR-008.

Why synchronous and what the trade-offs are: see the docstring in
`helpers/run_executor.py`.
"""
from __future__ import annotations

import json
from collections.abc import Mapping

from werkzeug import Request, Response

from dify_plugin.interfaces.endpoint import Endpoint

from helpers.backend_client import BackendClient, BackendClientError
from helpers.run_executor import execute_scenario_run


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

        try:
            with BackendClient(token=token, base_url=base_url) as client:
                result = execute_scenario_run(
                    self.session,
                    client,
                    app_id=app_id,
                    scenario_id=scenario_id,
                )
        except BackendClientError as e:
            # create_run failures (auth / scenario not found / network)
            # surface as 4xx so caller can act. Per-turn errors don't
            # raise — they're folded into result.status='failed'.
            return _error(
                _http_status_for(e.status),
                "create_run_failed",
                f"Backend rejected create_run: {e}",
            )

        # 200 even for failed runs — the HTTP call itself succeeded; the
        # failure semantics are in the body. Mirrors Dify's own
        # convention where a 200 carries an error event in stream mode.
        return Response(
            response=json.dumps(result.to_payload()),
            status=200,
            content_type="application/json",
        )


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
