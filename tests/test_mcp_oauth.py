from urllib.parse import parse_qs, urlsplit
import pytest
import httpx2
from mcp.shared.auth import AuthorizationCodeResult
from mcp.client.auth import OAuthFlowError
from harness.mcp.oauth import create_oauth_provider, VaultTokenStorage


class Vault:
    def __init__(self): self.values = {}
    def get(self, key): return self.values.get(key)
    def set(self, key, value): self.values[key] = value


def oauth_fixture(*, invalid_state=False, invalid_issuer=False):
    observed = {"requests": [], "authorization": None}
    vault = Vault()

    async def redirect(url):
        observed["authorization"] = parse_qs(urlsplit(url).query)

    async def callback():
        return AuthorizationCodeResult(code="fixture-code",
            state="wrong-state" if invalid_state else observed["authorization"]["state"][0],
            iss="https://wrong.test" if invalid_issuer else "https://auth.test")

    async def validate_resource(server, resource):
        assert server == resource == "https://resource.test/mcp"

    def respond(request):
        observed["requests"].append((request.method, str(request.url)))
        url = str(request.url)
        if url == "https://resource.test/mcp":
            if request.headers.get("authorization") == "Bearer fixture-token":
                return httpx2.Response(200, json={"authenticated": True})
            return httpx2.Response(401, headers={"WWW-Authenticate": 'Bearer resource_metadata="https://resource.test/.well-known/oauth-protected-resource"'})
        if url == "https://resource.test/.well-known/oauth-protected-resource":
            return httpx2.Response(200, json={"resource": "https://resource.test/mcp", "authorization_servers": ["https://auth.test"]})
        if url == "https://auth.test/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": "https://auth.test", "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token", "registration_endpoint": "https://auth.test/register",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True})
        if url == "https://auth.test/register":
            import json
            registration = json.loads(request.content)
            return httpx2.Response(201, json={**registration, "client_id": "fixture-client"})
        if url == "https://auth.test/token":
            payload = parse_qs(request.content.decode())
            assert payload["code"] == ["fixture-code"]
            assert len(payload["code_verifier"][0]) >= 43
            assert observed["authorization"]["code_challenge_method"] == ["S256"]
            return httpx2.Response(200, json={"access_token": "fixture-token", "token_type": "Bearer", "expires_in": 3600,
                                              "refresh_token": "fixture-refresh"})
        raise AssertionError("Unexpected OAuth fixture request: " + url)

    provider = create_oauth_provider(endpoint="https://resource.test/mcp", redirect_uri="http://127.0.0.1:8765/callback",
        storage=VaultTokenStorage(vault, "owner:resource"), redirect_handler=redirect,
        callback_handler=callback, validate_resource_url=validate_resource)
    return provider, httpx2.MockTransport(respond), vault, observed


@pytest.mark.asyncio
async def test_mp05_oauth_pkce_discovery_registration_callback_and_vault():
    provider, transport, vault, observed = oauth_fixture()
    async with httpx2.AsyncClient(transport=transport, auth=provider) as client:
        result = await client.get("https://resource.test/mcp")
        assert result.status_code == 200 and result.json()["authenticated"]
    assert "owner:resource:tokens" in vault.values and "owner:resource:client" in vault.values
    assert sum(url.endswith("/token") for _, url in observed["requests"]) == 1
    # A second provider loads server-bound encrypted-vault tokens without reauthorizing.
    storage = VaultTokenStorage(vault, "owner:resource")
    assert (await storage.get_tokens()).access_token == "fixture-token"
    assert (await storage.get_client_info()).issuer == "https://auth.test"
    assert await VaultTokenStorage(vault, "different-owner:resource").get_tokens() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["invalid_state", "invalid_issuer"])
async def test_mp05_oauth_rejects_callback_state_and_issuer(invalid):
    provider, transport, vault, observed = oauth_fixture(**{invalid: True})
    async with httpx2.AsyncClient(transport=transport, auth=provider) as client:
        with pytest.raises(OAuthFlowError):
            await client.get("https://resource.test/mcp")
    assert "owner:resource:tokens" not in vault.values
    assert not any(url.endswith("/token") for _, url in observed["requests"])
