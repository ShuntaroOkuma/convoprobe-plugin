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
        token = (credentials or {}).get("convoprobe_api_token") or ""
        if not token:
            raise ToolProviderCredentialValidationError(
                "convoprobe_api_token is required"
            )

        base_url = (credentials or {}).get("convoprobe_api_base_url") or None

        client = BackendClient(token=token, base_url=base_url)
        try:
            client.health()
        except BackendClientError as e:
            if e.status == 401:
                raise ToolProviderCredentialValidationError(
                    "ConvoProbe rejected the token (401). Re-issue from "
                    "ConvoProbe Web UI -> Settings -> Plugin."
                ) from e
            raise ToolProviderCredentialValidationError(
                f"Could not reach ConvoProbe Backend: {e}"
            ) from e
        finally:
            client.close()
