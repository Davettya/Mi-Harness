"""Durable browser OAuth authorization; SDK handles subsequent authenticated I/O.

PKCE verifiers, OAuth state and authorization metadata are held in the OS vault.
The business repository stores references, ownership, expiry and one-use status.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from harness.core import HarnessError, content_hash
from .client_factory import resolve
from .oauth import CredentialTokenStorage


def _origin(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class OAuthAuthorizationService:
    def __init__(self, store, vault, profile_resolver, *, access_factory, authorize_url,
                 callback_url: str, transport=None, ttl_seconds=600):
        self.store, self.vault, self.profile_resolver = store, vault, profile_resolver
        self.access_factory, self.authorize_url = access_factory, authorize_url
        self.callback_url, self.transport, self.ttl_seconds = callback_url, transport, ttl_seconds
        parsed = urlsplit(callback_url)
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") or parsed.scheme != "http" or parsed.fragment or parsed.query:
            raise ValueError("local OAuth callback must be a fixed loopback HTTP URL")

    async def _request(self, method, url, context, *, expected=(200,), **kwargs):
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
            raise HarnessError("OAUTH_ENDPOINT", "OAuth 端点必须使用 HTTPS 或经批准的 loopback", 403)
        if parsed.username or parsed.password or parsed.fragment:
            raise HarnessError("OAUTH_ENDPOINT", "OAuth 端点格式不安全", 403)
        await resolve(self.authorize_url(url, context, "oauth"))
        async with httpx2.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url, **kwargs)
            if response.status_code not in expected:
                raise HarnessError("OAUTH_HTTP", "OAuth 服务请求失败", 502)
            if len(response.content) > 1_000_000:
                raise HarnessError("OAUTH_METADATA_LIMIT", "OAuth 响应超过限制", 502)
            return response

    async def authorize(self, owner_id: str, server_id: str):
        profile = await resolve(self.profile_resolver(server_id))
        if profile.transport == "stdio":
            raise HarnessError("OAUTH_TRANSPORT", "stdio 服务不使用浏览器 OAuth", 422)
        context = await resolve(self.access_factory(owner_id, server_id))
        endpoint = profile.endpoint
        base = _origin(endpoint)
        path = urlsplit(endpoint).path.rstrip("/")
        prm_url = base + "/.well-known/oauth-protected-resource" + path
        response = await self._request("GET", prm_url, context, expected=(200, 404))
        if response.status_code == 404 and path:
            response = await self._request("GET", base + "/.well-known/oauth-protected-resource", context, expected=(200, 404))
        resource, issuer = endpoint, base
        if response.status_code == 200:
            metadata = response.json()
            if metadata.get("resource", "").rstrip("/") != endpoint.rstrip("/"):
                raise HarnessError("OAUTH_RESOURCE_MISMATCH", "受保护资源元数据属于另一服务", 403)
            issuers = metadata.get("authorization_servers") or []
            if not issuers or not isinstance(issuers[0], str):
                raise HarnessError("OAUTH_ISSUER_MISSING", "资源未声明授权服务器", 422)
            issuer = issuers[0].rstrip("/")
            resource = metadata["resource"]
        issuer_parts = urlsplit(issuer)
        authorization_metadata = await self._request("GET", _origin(issuer) + "/.well-known/oauth-authorization-server" + issuer_parts.path.rstrip("/"), context)
        metadata = authorization_metadata.json()
        if metadata.get("issuer", "").rstrip("/") != issuer:
            raise HarnessError("OAUTH_ISSUER_MISMATCH", "授权服务器 issuer 与发现结果不一致", 403)
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise HarnessError("OAUTH_PKCE_REQUIRED", "授权服务器必须支持 S256 PKCE", 422)
        authorization_endpoint, token_endpoint = metadata.get("authorization_endpoint"), metadata.get("token_endpoint")
        if not authorization_endpoint or not token_endpoint:
            raise HarnessError("OAUTH_METADATA_INVALID", "授权服务器缺少必要端点", 422)
        # Explicitly authorize destinations before displaying a redirect or exchanging secrets.
        for destination in (authorization_endpoint, token_endpoint):
            await resolve(self.authorize_url(destination, context, "oauth"))
        storage = CredentialTokenStorage(self.vault, self.store, owner_id=owner_id, server_id=server_id)
        existing = await storage.get_client_info()
        if existing and existing.issuer and existing.issuer.rstrip("/") == issuer:
            client_info = existing
        else:
            registration = metadata.get("registration_endpoint")
            if not registration:
                raise HarnessError("OAUTH_CLIENT_REGISTRATION", "服务需预先配置客户端或开放动态注册", 422)
            client_metadata = OAuthClientMetadata(client_name="Mi Harness", redirect_uris=[self.callback_url],
                grant_types=["authorization_code", "refresh_token"], response_types=["code"], token_endpoint_auth_method="none")
            registered = await self._request("POST", registration, context, expected=(200, 201), json=client_metadata.model_dump(mode="json", exclude_none=True))
            client_info = OAuthClientInformationFull.model_validate(registered.json())
            if client_info.token_endpoint_auth_method not in (None, "none"):
                raise HarnessError("OAUTH_CLIENT_AUTH_UNSUPPORTED", "本地浏览器客户端要求 public client + PKCE", 422)
            client_info.issuer = issuer
            await storage.set_client_info(client_info)
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        identity = str(uuid.uuid4())
        flow = {"state": state, "verifier": verifier, "issuer": issuer, "token_endpoint": token_endpoint,
                "issuer_response_required": metadata.get("authorization_response_iss_parameter_supported", False),
                "client_id": client_info.client_id, "redirect_uri": self.callback_url, "resource": resource}
        vault_ref = self.vault.put("oauth-flow:" + identity, json.dumps(flow))
        self.store.compare_and_set("oauth_authorizations", state_hash, 0, {"authorization_id": identity,
            "owner_id": owner_id, "server_id": server_id, "config_revision": profile.config_revision,
            "vault_ref": vault_ref, "status": "pending", "expires_at": time.time() + self.ttl_seconds, "revision": 1})
        query = {"response_type": "code", "client_id": client_info.client_id, "redirect_uri": self.callback_url,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "resource": resource}
        url = authorization_endpoint + ("&" if "?" in authorization_endpoint else "?") + urlencode(query)
        return {"authorization_id": identity, "authorization_url": url, "expires_in": self.ttl_seconds,
                "server_id": server_id, "status": "awaiting_browser"}

    async def callback(self, params: dict):
        state = params.get("state")
        if not isinstance(state, str) or not state or len(state) > 512:
            raise HarnessError("OAUTH_STATE", "OAuth 回调 state 无效", 403)
        key = hashlib.sha256(state.encode()).hexdigest()
        record = self.store.get("oauth_authorizations", key)
        if not record or record["status"] != "pending" or record["expires_at"] <= time.time():
            raise HarnessError("OAUTH_STATE", "OAuth 回调已使用、过期或不属于此实例", 403)
        flow = json.loads(self.vault.get(record["vault_ref"], "oauth-flow:" + record["authorization_id"]))
        if not secrets.compare_digest(flow["state"], state):
            raise HarnessError("OAUTH_STATE", "OAuth state 不匹配", 403)
        issuer = params.get("iss")
        if (issuer is not None and issuer.rstrip("/") != flow["issuer"]) or (flow["issuer_response_required"] and not issuer):
            raise HarnessError("OAUTH_ISSUER_MISMATCH", "OAuth 回调 issuer 不匹配", 403)
        if params.get("error") or not isinstance(params.get("code"), str) or not params["code"]:
            self.store.compare_and_set("oauth_authorizations", key, record["revision"], {**record, "revision": record["revision"] + 1, "status": "denied"})
            raise HarnessError("OAUTH_DENIED", "授权未完成", 422)
        profile = await resolve(self.profile_resolver(record["server_id"]))
        if profile.config_revision != record["config_revision"]:
            raise HarnessError("OAUTH_CONFIG_CHANGED", "授权期间服务配置已变化，请重新授权", 409)
        claimed = self.store.compare_and_set("oauth_authorizations", key, record["revision"],
            {**record, "revision": record["revision"] + 1, "status": "exchanging"})
        context = await resolve(self.access_factory(record["owner_id"], record["server_id"]))
        try:
            response = await self._request("POST", flow["token_endpoint"], context, data={"grant_type": "authorization_code",
                "code": params["code"], "redirect_uri": flow["redirect_uri"], "client_id": flow["client_id"],
                "code_verifier": flow["verifier"], "resource": flow["resource"]})
            token = OAuthToken.model_validate(response.json())
            storage = CredentialTokenStorage(self.vault, self.store, owner_id=record["owner_id"], server_id=record["server_id"])
            await storage.set_tokens(token)
            self.store.compare_and_set("oauth_authorizations", key, claimed["revision"],
                {**claimed, "revision": claimed["revision"] + 1, "status": "authorized", "vault_ref": None})
            self.vault.delete(record["vault_ref"], "oauth-flow:" + record["authorization_id"])
            return {"server_id": record["server_id"], "owner_id": record["owner_id"], "status": "authorized",
                    "oauth_ref": "oauth:" + record["server_id"]}
        except Exception:
            current = self.store.get("oauth_authorizations", key)
            if current and current["status"] == "exchanging":
                self.store.compare_and_set("oauth_authorizations", key, current["revision"],
                    {**current, "revision": current["revision"] + 1, "status": "failed_requires_reauthorization"})
            raise

    async def resolve(self, oauth_ref, profile, context):
        if oauth_ref != "oauth:" + profile.server_id:
            raise HarnessError("OAUTH_SCOPE", "OAuth 引用绑定到另一服务", 403)
        storage = CredentialTokenStorage(self.vault, self.store, owner_id=context.owner_id, server_id=profile.server_id)
        tokens, info = await storage.get_tokens(), await storage.get_client_info()
        if not tokens or not info:
            raise HarnessError("OAUTH_REQUIRED", "请先在 MCP 设置中完成授权", 401)
        metadata = OAuthClientMetadata.model_validate(info.model_dump())
        async def resource_matches(server, resource):
            await resolve(self.authorize_url(server, context, "oauth"))
            if resource and resource.rstrip("/") != profile.endpoint.rstrip("/"):
                raise HarnessError("OAUTH_RESOURCE_MISMATCH", "OAuth 凭据不能透传到另一资源", 403)
        # Missing browser handlers deliberately force explicit UI reauthorization on refresh failure.
        return OAuthClientProvider(profile.endpoint, client_metadata=metadata, storage=storage,
                                   validate_resource_url=resource_matches)
