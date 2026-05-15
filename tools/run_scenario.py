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

        client = BackendClient(token=token, base_url=base_url)
        try:
            items = client.list_scenarios()
        except BackendClientError:
            # Backend may not yet expose /scenarios (spike phase). Return
            # an empty list rather than raising — the user will see
            # "no scenarios" and can investigate via the ConvoProbe Web
            # UI rather than the tool config panel breaking outright.
            return []
        finally:
            client.close()

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
        """Spike implementation: echo parameters so the node integration is
        verifiable in Dify Studio without depending on the (yet-to-be-built)
        Tool→Endpoint reuse path. Replaced by the real run loop in T39.
        """
        scenario_id = tool_parameters.get("scenario_id") or ""
        target_app = tool_parameters.get("target_app") or {}
        if isinstance(target_app, dict):
            app_id = target_app.get("app_id") or ""
        else:
            app_id = ""
        wait_for_completion = bool(tool_parameters.get("wait_for_completion", True))

        yield self.create_json_message({
            "spike": True,
            "note": "Tool wired; actual run execution lands in T39",
            "scenario_id": scenario_id,
            "target_app_id": app_id,
            "wait_for_completion": wait_for_completion,
        })
