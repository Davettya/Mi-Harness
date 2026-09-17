"""OAuth flows are delegated to the official SDK and platform credential vault."""
from __future__ import annotations

from urllib.parse import urlsplit
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientMetadata, OAuthClientInformationFull, OAuthToken
from .client_factory import resolve
from harness.core.contracts import content_hash


class VaultTokenStorage:
    """Vault keys are server+principal scoped. Values never enter profile/snapshot."""
    def __init__(self, vault, namespace: str):
        self.vault, self.namespace = vault, namespace

    async def get_tokens(self):
        value = await resolve(self.vault.get(self.namespace + ":tokens"))
        return OAuthToken.model_validate_json(value) if value else None

    async def set_tokens(self, tokens):
        await resolve(self.vault.set(self.namespace + ":tokens", tokens.model_dump_json()))

    async def get_client_info(self):
        value = await resolve(self.vault.get(self.namespace + ":client"))
        return OAuthClientInformationFull.model_validate_json(value) if value else None

    async def set_client_info(self, info):
        await resolve(self.vault.set(self.namespace + ":client", info.model_dump_json()))


class CredentialTokenStorage(VaultTokenStorage):
    """Adapter to the platform OS CredentialVault; only refs are stored in SQLite."""
    def __init__(self, vault, repository, *, owner_id: str, server_id: str):
        namespace = content_hash({"owner": owner_id, "server": server_id})
        target = "mcp-oauth-" + namespace

        class ScopedVault:
            def get(self, key):
                entry = repository.get("oauth_credential_refs", key)
                return vault.get(entry["credential_ref"], target) if entry else None

            def set(self, key, value):
                old = repository.get("oauth_credential_refs", key)
                reference = vault.put(target, value)
                try:
                    repository.put("oauth_credential_refs", key, {"credential_ref": reference,
                        "owner_id": owner_id, "server_id": server_id, "revision": (old or {}).get("revision", 0) + 1})
                except Exception:
                    vault.delete(reference, target)
                    raise
                if old:
                    vault.delete(old["credential_ref"], target)

        super().__init__(ScopedVault(), namespace)


def create_oauth_provider(*, endpoint: str, redirect_uri: str, storage, redirect_handler,
                          callback_handler, validate_resource_url, client_name="Local Agent Harness"):
    callback = urlsplit(redirect_uri)
    if callback.scheme not in ("http", "https") or not callback.hostname or callback.username or callback.fragment:
        raise ValueError("invalid OAuth callback URL")
    if callback.scheme == "http" and callback.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("OAuth HTTP callback must be loopback")
    return OAuthClientProvider(server_url=endpoint,
        client_metadata=OAuthClientMetadata(client_name=client_name, redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            token_endpoint_auth_method="none"), storage=storage, redirect_handler=redirect_handler,
        callback_handler=callback_handler, validate_resource_url=validate_resource_url)
