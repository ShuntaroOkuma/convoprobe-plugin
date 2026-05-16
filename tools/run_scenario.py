"""Run Scenario tool — the Tool-plugin entrypoint (PRD v0.2 F1/F4/F5).

Spike scope (T38)
-----------------
This file exists to verify the Tool-plugin scaffolding works end-to-end
inside Dify Studio:

- `_fetch_parameter_options("scenario_id")` is called by Dify when the
  user opens the scenario dropdown in the node config UI. We exercise the
  ConvoProbe Backend round-trip so failures surface during the spike,
  not in production.
- `_invoke` echoes the chosen parameters as a JSON message. Wiring it to
  the real scenario-execution path (the existing `/run` endpoint's loop
  in ``endpoints/run.py``) is T39; doing it here would mix verification
  with production logic and bloat the spike PR.

The Tool deliberately reuses the same ``BackendClient`` and credential
schema as the Endpoint flow so we never grow a second authentication
surface (ADR-008).
"""
from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities import I18nObject, ParameterOption
from dify_plugin.entities.tool import ToolInvokeMessage

from helpers.backend_client import BackendClient, BackendClientError
from helpers.run_executor import execute_scenario_run


class RunScenarioTool(Tool):
    def _fetch_parameter_options(self, parameter: str) -> list[ParameterOption]:
        """Populate the dynamic dropdown for `scenario_id`.

        Other parameters are not dynamic-select; Dify should not call us
        for them, but we return [] defensively so an SDK quirk cannot
        crash the node config UI.
        """
        if parameter != "scenario_id":
            return []

        creds = self.runtime.credentials or {}
        token = creds.get("convoprobe_api_token") or ""
        if not token:
            return []
        base_url = creds.get("convoprobe_api_base_url") or None

        try:
            with BackendClient(token=token, base_url=base_url) as client:
                items = client.list_scenarios()
        except BackendClientError:
            # Backend may not yet expose /scenarios (spike phase). Return
            # an empty list rather than raising — the user will see
            # "no scenarios" and can investigate via the ConvoProbe Web
            # UI rather than the tool config panel breaking outright.
            return []

        # I18nObject in the SDK currently only models en_US/zh_Hans/pt_BR
        # (see dify_plugin/entities/__init__.py); ja_JP passed here would
        # be silently dropped by pydantic. Static YAML labels still
        # support ja_JP, so the rest of the UI remains localized — only
        # dynamic dropdown rows render in en_US.
        return [
            ParameterOption(
                value=item["id"],
                label=I18nObject(en_US=item["name"]),
            )
            for item in items
        ]

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
        scenario_id = tool_parameters.get("scenario_id") or ""
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
                "error": "scenario_id is required (pick one from the dropdown).",
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
