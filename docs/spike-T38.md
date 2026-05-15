# Spike T38 — Tool Plugin Scaffolding

**Date**: 2026-05-15
**Branch**: `feat/tool-plugin-spike`
**Owner**: Implementation Agent (Plugin)
**Related**: [PRD v0.2 §7.3](../../convoprobe/convoprobe-docs/requirements/dify-plugin-prd.md), [ADR-008](../../convoprobe/convoprobe-docs/design/dify-plugin-architecture.md)

## Goal

Before committing to the full Tool-plugin MVP (PRD v0.2), de-risk the three
items called out in PRD v0.2 §7.3 / ADR-008 by building minimal scaffolding
and probing the SDK:

1. **R6** — Does the Dify Plugin SDK actually support a dynamic dropdown
   that the Tool node config UI can populate at edit time? (PRD F4)
2. **R7** — Does `app-selector` work as a Tool parameter, or is it only an
   Endpoint-context primitive? (PRD F2)
3. **Tool + Endpoint coexistence** — Can a single plugin manifest declare
   both `plugins.tools` and `plugins.endpoints` so the existing `/run`
   endpoint can stay in place while the Tool is added on top?

## Findings (all green)

| Risk | Result | Evidence |
|---|---|---|
| R6 dynamic dropdown | ✅ Supported via `dynamic-select` parameter type and `Tool._fetch_parameter_options(name)` override | `dify_plugin.entities.tool.ToolParameter.ToolParameterType.DYNAMIC_SELECT` (entities/tool.py:97); `Tool._fetch_parameter_options` (interfaces/tool/__init__.py:344) |
| R7 app-selector in Tool context | ✅ Supported via `app-selector` parameter type with optional `scope` filter | `ToolParameterType.APP_SELECTOR` (entities/tool.py:91); jina/anthropic plugins use it as a Tool param |
| Tool + Endpoint coexistence | ✅ Manifest schema declares `plugins.tools: list[str]` and `plugins.endpoints: list[str]` independently | `dify_plugin/core/entities/plugin/setup.py` lines 108-120 |

Validation evidence: `python -c "from dify_plugin.core.entities.plugin.setup import PluginConfiguration; ..."` against the updated `manifest.yaml` and `provider/convoprobe.yaml` parses both lists cleanly, with `tool.enabled=True` permission honored.

## What changed in this spike

```
convoprobe-plugin/
├── manifest.yaml                          # + tool permission + provider path
├── provider/                              # NEW
│   ├── convoprobe.yaml                    # ToolProvider config (identity, credentials, tools list)
│   └── convoprobe.py                      # ConvoProbeProvider._validate_credentials → backend /health
├── tools/                                 # NEW
│   ├── run_scenario.yaml                  # Tool config: scenario_id (dynamic-select) + target_app (app-selector) + wait_for_completion
│   └── run_scenario.py                    # RunScenarioTool with _fetch_parameter_options + spike _invoke
├── helpers/
│   └── backend_client.py                  # + list_scenarios() method
└── tests/
    ├── helpers/test_backend_client.py     # + 4 list_scenarios tests
    ├── provider/test_convoprobe.py        # NEW (5 tests)
    └── tools/test_run_scenario.py         # NEW (8 tests)
```

Test result: **65 passed, 0 failed**.

The existing `endpoints/run.py` + `group/convoprobe.yaml` Endpoint flow is untouched. Both surfaces will coexist in 0.0.3.

## Unexpected discoveries

### `I18nObject` Python class does not model `ja_JP`

`dify_plugin.entities.I18nObject` exposes only `en_US`, `zh_Hans`, `pt_BR` (see `entities/__init__.py:9-26`). Passing `ja_JP=` to its constructor is silently dropped by pydantic.

**Impact**: Static labels written in YAML files (e.g. `tools/run_scenario.yaml`) keep their `ja_JP` field — the YAML loader passes through extras. **But** any label constructed in Python (such as a dynamic-dropdown row built inside `_fetch_parameter_options`) renders in `en_US` only.

**Mitigation**: For dynamic dropdown rows we now pass only `en_US`. For scenario names that need to be displayed differently in JA vs EN, either localize on the Backend side before returning, or let the SDK upstream catch up. Recorded in `tools/run_scenario.py` as an inline comment.

**Follow-up**: file an SDK issue / PR to add `ja_JP` to `I18nObject` once we are sure the Marketplace flow doesn't have a separate path. Not blocking for MVP.

### Backend `/scenarios` endpoint path

PRD v0.2 §5.3 called the new endpoint `GET /api/v1/plugin/scenarios`. The Plugin authenticates with the `cp_<token>` Plugin token, which is the auth scheme for `/api/internal/plugin/*` — not the public JWT-protected `/api/v1/*` family. Sticking the new route under `/api/internal/plugin/scenarios` reuses the existing `PluginAuth` middleware and matches every other plugin call site.

**Action**: implement at `/api/internal/plugin/scenarios` in the Backend MVP task (T39 backend leg). PRD §5.3 wording will be corrected in v0.2.1 next time the doc is touched.

## What the spike intentionally did NOT do

- `RunScenarioTool._invoke` only echoes parameters as a JSON message. The real run loop — reusing `endpoints/run.py`'s scenario-execution code — lands in **T39**. Doing it here would have mixed scaffold verification with production-path refactoring and bloated the diff.
- The Backend `/api/internal/plugin/scenarios` endpoint is **not implemented**. `list_scenarios()` currently 404s in production; `_fetch_parameter_options` swallows the error and shows an empty dropdown. The Backend leg of T39 will close this.
- Live testing inside Dify Studio (Marketplace install or Debug Mode) is **not** part of T38. The spike confirmed the SDK contract; visual confirmation is part of T39's QA.

## Next tasks (T39 onward)

| ID | Stream | What |
|---|---|---|
| T39.1 | backend | Add `GET /api/internal/plugin/scenarios` returning `{scenarios: [{id, name}]}` for the authenticated installation's user |
| T39.2 | plugin | Extract the synchronous scenario-execution loop from `endpoints/run.py` into a helper, and call it from `RunScenarioTool._invoke`. Update the Tool to return real `run_id`, `status`, `score`, `transcript_url` |
| T39.3 | plugin | Add `wait_for_completion: false` fast-path returning `run_id` immediately (still synchronous, but Tool returns before the run loop drains) |
| T39.4 | plugin | Live verification in Dify Studio Debug Mode: load `manifest.yaml`, verify the Tool node shows in the workflow palette, scenario dropdown populates, app-selector picks a chat app, an end-to-end run completes and the JSON output is wired downstream |
| T39.5 | plugin | i18n pass on the new YAMLs (ja_JP/en_US strings); decide on the Marketplace screenshot set |
