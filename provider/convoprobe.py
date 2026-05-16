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
        # Dify Studio wraps every plugin exception in a PluginInvokeError
        # JSON envelope (see dify_plugin/core/server/io_server.py:117).
        # The visible result is roughly:
        #   PluginInvokeError: {"args":{},"error_type":"...Error","message":"..."}
        # The error_type and JSON syntax eat ~80 characters of visual
        # budget before our message starts, and Dify Studio truncates
        # long lines. So we keep messages short enough that the action
        # survives the truncation. Avoiding `>` because JSON escapes it
        # to `>` in the displayed envelope.
        token = (credentials or {}).get("convoprobe_api_token") or ""
        if not token:
            raise ToolProviderCredentialValidationError(
                "ConvoProbe token required. Get one in Web UI Settings, then Plugin."
            )

        base_url = (credentials or {}).get("convoprobe_api_base_url") or None

        try:
            with BackendClient(token=token, base_url=base_url) as client:
                client.health()
        except BackendClientError as e:
            if e.status == 401:
                raise ToolProviderCredentialValidationError(
                    "ConvoProbe token rejected. Reissue in Web UI Settings, then Plugin."
                ) from e
            raise ToolProviderCredentialValidationError(
                f"ConvoProbe Backend unreachable. Check the API Base URL above. ({e})"
            ) from e
