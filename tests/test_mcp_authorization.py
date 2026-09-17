import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx2
import pytest
from harness.core import HarnessError
from harness.storage import Store
from harness.mcp import ConnectionProfile, OAuthAuthorizationService


class FixtureVault:
    def __init__(self): self.values = {}
    def put(self, target, value):
        reference = "credential:" + target + ":" + uuid.uuid4().hex
        self.values[reference] = target, value
        return reference
    def get(self, reference, target):
        actual, value = self.values[reference]
        if actual != target: raise PermissionError("credential scope")
        return value
    def delete(self, reference, target):
        self.get(reference, target)
        del self.values[reference]


def auth_service(store, vault, observed):
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="streamable_http", endpoint="https://resource.test/mcp")
    def respond(request):
        path = urlsplit(str(request.url)).path
        if path.startswith("/.well-known/oauth-protected-resource"):
            return httpx2.Response(200, json={"resource": profile.endpoint, "authorization_servers": ["https://auth.test"]})
        if path == "/.well-known/oauth-authorization-server":
            return httpx2.Response(200, json={"issuer": "https://auth.test", "authorization_endpoint": "https://auth.test/authorize",
                "token_endpoint": "https://auth.test/token", "registration_endpoint": "https://auth.test/register",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True})
        if path == "/register":
            return httpx2.Response(201, json={**json.loads(request.content), "client_id": "client-fixture"})
        if path == "/token":
            observed.append(parse_qs(request.content.decode()))
            return httpx2.Response(200, json={"access_token": "private-canary-token", "token_type": "Bearer", "expires_in": 3600, "refresh_token": "private-refresh"})
        raise AssertionError(path)
    return OAuthAuthorizationService(store, vault, lambda _: profile, access_factory=lambda owner, _: SimpleNamespace(owner_id=owner),
        authorize_url=lambda *args: None, callback_url="http://127.0.0.1:8767/api/mcp/oauth/callback", transport=httpx2.MockTransport(respond))


@pytest.mark.asyncio
async def test_mp05_durable_oauth_restart_state_issuer_replay_and_no_plaintext(tmp_path):
    store, vault, observed = Store(tmp_path / "app.db"), FixtureVault(), []
    service = auth_service(store, vault, observed)
    pending = await service.authorize("owner", "fixture")
    query = parse_qs(urlsplit(pending["authorization_url"]).query)
    state = query["state"][0]
    restarted = auth_service(Store(tmp_path / "app.db"), vault, observed)
    with pytest.raises(HarnessError):
        await restarted.callback({"state": state, "code": "code", "iss": "https://attacker.test"})
    assert not observed
    result = await restarted.callback({"state": state, "code": "private-auth-code", "iss": "https://auth.test"})
    assert result["owner_id"] == "owner" and result["status"] == "authorized"
    verifier = observed[0]["code_verifier"][0]
    import base64
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == query["code_challenge"][0]
    with pytest.raises(HarnessError):
        await restarted.callback({"state": state, "code": "private-auth-code", "iss": "https://auth.test"})
    assert len(observed) == 1
    serialized = json.dumps(store.list("oauth_authorizations") + store.list("oauth_credential_refs"))
    for secret in (state, verifier, "private-canary-token", "private-refresh", "private-auth-code"):
        assert secret not in serialized
    profile = restarted.profile_resolver("fixture")
    assert await restarted.resolve("oauth:fixture", profile, SimpleNamespace(owner_id="owner"))
    with pytest.raises(HarnessError):
        await restarted.resolve("oauth:fixture", profile, SimpleNamespace(owner_id="other"))
