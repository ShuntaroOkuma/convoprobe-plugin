"""ConvoProbe tool provider — credentials validation for the Tool plugin
flow (PRD v0.2, ADR-008).

Credentials are validated by calling ConvoProbe Backend `/api/internal/plugin/health`,
which mirrors the validation path the Endpoint flow uses. If the token is
invalid or revoked the backend returns 401; we surface that as a
ToolProviderCredentialValidationError so Dify Studio shows a useful error
message at install time instead of waiting until first invocation.
"""
from __future__ import annotations

from typing import Any

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from helpers.backend_client import BackendClient, BackendClientError


class ConvoProbeProvider(ToolProvider):
    def _validate_credentials(self, credentials: dict[str, Any]) -> None:
        # Error messages are deliberately action-first and avoid the `>`
        # character (Dify Studio surfaces the message wrapped in a JSON
        # blob, which escapes `>` to `>` and makes the breadcrumb
        # unreadable). The first sentence is the user's next step so the
        # message remains useful even when truncated by the UI.
        token = (credentials or {}).get("convoprobe_api_token") or ""
        if not token:
            raise ToolProviderCredentialValidationError(
                "ConvoProbe API token is required. "
                "Open the ConvoProbe Web UI, go to Settings then Plugin, "
                "and paste a token starting with cp_."
            )

        base_url = (credentials or {}).get("convoprobe_api_base_url") or None

        client = BackendClient(token=token, base_url=base_url)
        try:
            client.health()
        except BackendClientError as e:
            if e.status == 401:
                raise ToolProviderCredentialValidationError(
                    "Invalid ConvoProbe API token. "
                    "Issue a fresh one in the ConvoProbe Web UI under "
                    "Settings then Plugin (starts with cp_), then paste it here."
                ) from e
            raise ToolProviderCredentialValidationError(
                "Cannot reach ConvoProbe Backend at the configured API Base URL. "
                "Verify the URL and network connectivity. "
                f"Detail: {e}"
            ) from e
        finally:
            client.close()
