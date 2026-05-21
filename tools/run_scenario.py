"""Run Scenario tool — the Tool-plugin entrypoint (PRD v0.2 F1/F4/F5).

Scope
-----
Drives a ConvoProbe scenario synchronously inside a Dify workflow node
and yields the terminal payload (run_id, status, completed_turns,
transcript_url, ...). The actual run loop lives in
``helpers.run_executor.execute_scenario_run`` and is shared with the
existing /run Endpoint so both trigger paths converge on a single
implementation (ADR-008).

scenario_id as a static input (Dify dynamic-select regression workaround)
-------------------------------------------------------------------------
The original design (PRD v0.2 F4, Spike T38) used
``type: dynamic-select`` for ``scenario_id`` with a
``_fetch_parameter_options`` hook that called Backend
``/api/internal/plugin/scenarios``. SDK + manifest were correct and the
Backend endpoint worked end-to-end via curl, but Dify Cloud Studio's
Tool-plugin dynamic-select stopped firing in Dify 1.6.0 (regression
documented in langgenius/dify#22147, still unfixed in 2026-05).
Symptom: clicking the dropdown produced no network call at all.

Falling back to a plain text input for ``scenario_id`` keeps the Tool
usable today. ``BackendClient.list_scenarios()`` is intentionally left
in place; once Dify ships the dynamic-select fix we flip the YAML back
to ``type: dynamic-select`` and re-add a ~10-line
``_fetch_parameter_options`` here (see git log for ``feat/tool-real-run``
branch — the original implementation is recoverable verbatim).
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage

from helpers.backend_client import BackendClient, BackendClientError
from helpers.run_executor import execute_scenario_run


class RunScenarioTool(Tool):
    def _invoke(
        self,
        tool_parameters: dict[str, Any],
    ) -> Generator[ToolInvokeMessage, None, None]:
        """Drive a scenario synchronously and return the terminal payload.

        Shares `helpers.run_executor.execute_scenario_run` with the
        existing /run Endpoint so Tool callers and curl/HTTP-node
        callers see exactly the same shape. `wait_for_completion=False`
        is currently a no-op (always synchronous) — fast-path returning
        only `run_id` is T39.3.
        """
        scenario_id = (tool_parameters.get("scenario_id") or "").strip()
        # `app-selector` returns a dict ({app_id, app_type, ...}) when the
        # user picks from the dropdown. Inside a workflow the same slot
        # can be fed by a variable or another node's output, in which
        # case Dify hands us the raw app_id string. Accept both rather
        # than silently dropping the latter.
        target_app = tool_parameters.get("target_app")
        if isinstance(target_app, dict):
            app_id = target_app.get("app_id") or ""
        elif isinstance(target_app, str):
            app_id = target_app
        else:
            app_id = ""

        if not scenario_id:
            yield self.create_json_message({
                "error": (
                    "scenario_id is required. Paste the UUID from a "
                    "ConvoProbe Web UI scenario URL."
                ),
            })
            return
        if not app_id:
            yield self.create_json_message({
                "error": "target_app is required (pick a Dify chatbot from the selector).",
            })
            return

        creds = self.runtime.credentials or {}
        token = creds.get("convoprobe_api_token") or ""
        base_url = creds.get("convoprobe_api_base_url") or None
        if not token:
            yield self.create_json_message({
                "error": "ConvoProbe API token missing from plugin credentials.",
            })
            return

        try:
            with BackendClient(token=token, base_url=base_url) as client:
                result = execute_scenario_run(
                    self.session,
                    client,
                    app_id=app_id,
                    scenario_id=scenario_id,
                )
        except BackendClientError as e:
            # create_run failed (auth / scenario not found / network).
            # Surface as a structured error message rather than raising —
            # Dify Studio shows yielded JSON inline in the workflow run
            # panel, exceptions get wrapped in the noisy PluginInvokeError
            # envelope.
            yield self.create_json_message({
                "error": f"Backend rejected create_run: {e}",
                "status_code": e.status,
            })
            return

        yield self.create_json_message(result.to_payload())
