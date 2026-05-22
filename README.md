# ConvoProbe Dify Plugin

Multi-turn chatbot QA testing for Dify chatbots. Run scenario tests against your apps in Dify Studio and view results in [ConvoProbe](https://convoprobe.vercel.app).

## What it does

- Reads a multi-turn test scenario stored in ConvoProbe
- Executes it against a Dify chatbot in your workspace via Reverse Invocation
- Maintains conversation continuity across turns (`conversation_id`)
- Posts results back to ConvoProbe for LLM-as-Judge scoring and review

## Requirements

- Dify v1.0+ (Plugin runtime support)
- A ConvoProbe account ([sign up](https://convoprobe.vercel.app))
- A Chat-type App in the same Dify workspace

## Install

### Via Marketplace (recommended once published)

Search for "ConvoProbe" in Dify Studio → Plugins → Marketplace.

### Via local debug (developer)

1. `cp .env.example .env.local` and fill `REMOTE_INSTALL_KEY` from Dify Studio → Plugins → Debug
2. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. `python -m main`
4. The plugin will appear under "Local debug plugins" in your workspace

## Configure

| Setting | Required | Description |
|---|---|---|
| `convoprobe_api_token` | Yes | Issue from ConvoProbe Web UI → Settings → Plugin |
| `target_app` | Yes | The Dify chatbot to test (workspace scope) |
| `judge_llm_api_key` | No | Optional fallback. Leave empty to use Dify's model provider |
| `convoprobe_api_base_url` | No | Self-hosted ConvoProbe URL (defaults to SaaS) |

## Privacy

See [PRIVACY.md](PRIVACY.md). The plugin transfers scenario inputs and chatbot responses to ConvoProbe Backend for evaluation. No raw API keys are stored on ConvoProbe.

## Development

```bash
make test       # pytest with coverage
make lint       # ruff
make package    # build .difypkg for Marketplace submission
```

## Source & Contact

- Repository: https://github.com/ShuntaroOkuma/convoprobe-plugin
- Issues: https://github.com/ShuntaroOkuma/convoprobe-plugin/issues
- Author: soitto (Shuntaro Okuma) — shuntaro.okuma@soitto.jp

## License

MIT — see [LICENSE](LICENSE).
