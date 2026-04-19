"""ConvoProbe /run endpoint.

Receives a run request from ConvoProbe Backend, executes the scenario
against the configured target Dify chatbot via Reverse Invocation,
and posts results back to Backend asynchronously.

This is a Day 1 scaffold. T20-T26 implements the full runtime.
"""
import json
import threading
from collections.abc import Mapping

from werkzeug import Request, Response

from dify_plugin.interfaces.endpoint import Endpoint


class RunEndpoint(Endpoint):
    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        try:
            payload = r.get_json(force=True, silent=True) or {}
        except Exception:
            return _error(400, "invalid_json", "Request body must be valid JSON")

        run_id = payload.get("run_id")
        scenario_id = payload.get("scenario_id")
        if not run_id or not scenario_id:
            return _error(400, "missing_field", "run_id and scenario_id are required")

        target_app = settings.get("target_app") or {}
        app_id = target_app.get("app_id") if isinstance(target_app, dict) else None
        if not app_id:
            return _error(400, "missing_app", "target_app is not configured")

        # T20-T22 will populate this with actual conversation loop.
        # Day 1 scaffold returns 'accepted' synchronously to validate the contract.
        threading.Thread(
            target=_noop_background,
            args=(run_id, scenario_id, app_id),
            daemon=True,
        ).start()

        return Response(
            response=json.dumps({
                "run_id": run_id,
                "status": "accepted",
                "message": "scaffold: full implementation in T20-T22",
            }),
            status=202,
            content_type="application/json",
        )


def _noop_background(run_id: str, scenario_id: str, app_id: str) -> None:
    pass


def _error(status: int, code: str, message: str) -> Response:
    return Response(
        response=json.dumps({"error": {"code": code, "message": message}}),
        status=status,
        content_type="application/json",
    )
