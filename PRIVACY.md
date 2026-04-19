# ConvoProbe Plugin — Privacy Notice

**Last updated**: 2026-04-19

## Data the plugin transfers

| Data | Direction | Purpose |
|---|---|---|
| Scenario definition (turns, expected behaviors) | ConvoProbe Backend → Plugin | To execute the test against your Dify chatbot |
| Chatbot prompts and responses | Plugin ↔ Dify Workspace (Reverse Invocation) | Multi-turn conversation execution |
| Transcript (full Q&A history of the run) | Plugin → ConvoProbe Backend | LLM-as-Judge scoring and result display |
| Latency / error metadata | Plugin → ConvoProbe Backend | Quality metrics |

## Data the plugin does NOT transfer

- Your Dify workspace API keys (the plugin uses the Dify-managed Reverse Invocation channel)
- Workspace member identities or unrelated app data
- Files or attachments outside the test scenario

## Storage on ConvoProbe

- Transcripts and scores are stored under your ConvoProbe account
- You can delete a run from ConvoProbe Web UI → Runs → Delete at any time
- ConvoProbe retains logs for 90 days for debugging; aggregated analytics are kept indefinitely (no PII)

## Token security

- The `convoprobe_api_token` you enter is stored encrypted by Dify
- Revoke at any time from ConvoProbe Web UI → Settings → Plugin → Revoke
- Tokens are bound to your ConvoProbe account and have user-scope only

## Contact

For privacy-related questions: [shuntaro.okuma@soitto.jp](mailto:shuntaro.okuma@soitto.jp)
