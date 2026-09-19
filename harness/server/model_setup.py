"""Paired-user model setup: three public inputs, bounded probes, immutable publication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from urllib.parse import urlsplit, urlunsplit

import httpx

from harness.core import HarnessError, canonical_json, new_id, utc_now
from harness.model_gateway import Capability, ModelGateway, ModelProfile, configured_model_limits
from harness.model_gateway.presets import PRESETS, provider_catalog
from harness.policy.engine import ModelSetupGrant
from harness.runtime.models import AgentSpec


def normalize_base_url(value: str) -> str:
    try:
        value = value.strip()
        parts = urlsplit(value)
        port = parts.port
        host = (parts.hostname or "").lower()
        if (
            parts.scheme not in {"https", "http"}
            or not host
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or "\\" in value
            or "%" in value
            or any(ord(c) < 33 for c in value)
            or any(piece in {".", ".."} for piece in parts.path.split("/"))
        ):
            raise ValueError
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if parts.scheme == "http" and not local:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port and port != (443 if parts.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))
    except (ValueError, AttributeError) as exc:
        raise HarnessError(
            "MODEL_BASE_URL_INVALID", "地址须为 HTTPS，或本机 HTTP；不能包含凭据、查询参数或片段", 422
        ) from exc


# Single owner for the complete setup suite; clients cannot accidentally omit a capability.
SETUP_CAPABILITIES = ("vision", "streaming")
SETUP_REQUIRED = ("transport", "text", "tool_calling", "tool_pairing")
SETUP_SUITE = "model-setup-v3"


def setup_options(body):
    if body.get("probe_mode") == "connectivity":
        return {"probe_mode": "connectivity", "optional_checks": []}
    return {"probe_mode": "agent", "optional_checks": list(SETUP_CAPABILITIES)}


class ModelSetupService:
    def __init__(self, services):
        self.services = services
        self.store = services.store
        self._active = 0
        self._recent = defaultdict(deque)
        self._cache = {}
        self._flights = {}
        self._fingerprint_key = secrets.token_bytes(32)
        self.verification_ttl = 600

    def _fingerprint(self, owner, profile, secret, options=None):
        config = {
            k: v
            for k, v in profile.model_dump(mode="json").items()
            if k not in {"profile_id", "revision", "credential_ref"}
        }
        return hmac.new(
            self._fingerprint_key,
            canonical_json(
                {
                    "owner": owner,
                    "config": config,
                    "secret": secret,
                    "suite": SETUP_SUITE,
                    "options": options or {},
                }
            ).encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _verified(self, owner, profile, secret, ctx, grant, token=None, options=None):
        key = self._fingerprint(owner, profile, secret, options)
        now = time.monotonic()
        self._cache = {k: v for k, v in self._cache.items() if v["expires"] > now}
        cached = self._cache.get(key)
        if token and (not cached or cached["token"] != token):
            raise HarnessError("MODEL_VERIFICATION_EXPIRED", "验证已失效或配置已变化，请重新验证", 409)
        if not self.services.settings.verification_reuse:
            # Disabling reuse forces fresh probes, but never bypasses receipt binding.
            cached = None
            self._cache.pop(key, None)
        if cached:
            return {**cached["report"], "profile_ref": profile.ref}, cached["token"], True
        if key not in self._flights:
            self._flights[key] = {
                "task": asyncio.create_task(self._probe(profile, secret, ctx, grant, **(options or {}))),
                "users": 0,
            }
        flight = self._flights[key]
        flight["users"] += 1
        started = time.monotonic()
        try:
            report = await asyncio.shield(flight["task"])
            cached = self._cache.get(key)
            if cached is None:
                checks = report["checks"]
                required = (
                    ("transport", "text")
                    if (options or {}).get("probe_mode") == "connectivity"
                    else SETUP_REQUIRED
                )
                ttl = self.verification_ttl if all(checks.get(name) for name in required) else 2
                cached = {"report": report, "token": new_id(), "expires": time.monotonic() + ttl}
                self._cache[key] = cached
            return {**report, "profile_ref": profile.ref}, cached["token"], False
        finally:
            flight["users"] -= 1
            if not flight["users"]:
                if not flight["task"].done():
                    flight["task"].cancel()
                    try:
                        await flight["task"]
                    except BaseException:
                        pass
                self._flights.pop(key, None)
            self.services.metrics.record(
                "model_setup.phase",
                {
                    "span_id": new_id(),
                    "phase": "probe",
                    "duration_ms": (time.monotonic() - started) * 1000,
                    "status": "completed" if key in self._cache else "interrupted",
                },
            )

    @contextmanager
    def _phase(self, name):
        started, status = time.monotonic(), "completed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self.services.metrics.record(
                "model_setup.phase",
                dict(
                    span_id=new_id(),
                    phase=name,
                    status=status,
                    duration_ms=(time.monotonic() - started) * 1000,
                ),
            )

    def providers(self):
        return provider_catalog()

    def _maintenance(self):
        if (self.store.get("system", "maintenance") or {}).get("enabled"):
            raise HarnessError("MAINTENANCE", "维护期间暂停模型配置", 503)

    @asynccontextmanager
    async def _admit(self, owner):
        self._maintenance()
        now = time.monotonic()
        queue = self._recent[owner]
        while queue and now - queue[0] >= 60:
            queue.popleft()
        if self._active >= 2 or len(queue) >= 12:
            raise HarnessError("MODEL_SETUP_RATE_LIMIT", "模型配置请求过于频繁，请稍后重试", 429)
        queue.append(now)
        self._active += 1
        try:
            with self._phase("total"):
                yield
        finally:
            self._active -= 1

    def _prepare(self, owner, body, *, need_model=False, saving=False):
        with self._phase("prepare"):
            return self._prepare_impl(owner, body, need_model=need_model, saving=saving)

    def _prepare_impl(self, owner, body, *, need_model=False, saving=False):
        if saving and body.get("activate", True) and self.services.settings.model_profile_ref:
            raise HarnessError(
                "MODEL_PROFILE_PINNED", "启动配置固定了默认模型；请先移除 model_profile_ref 固定项再保存", 409
            )
        preset = PRESETS.get(body.get("provider_id"))
        if not preset:
            raise HarnessError("MODEL_PROVIDER_UNKNOWN", "请选择受支持的提供方", 422)
        identity = body.get("profile_id")
        existing = self.store.get("config/models", identity) if identity else None
        if identity and not existing:
            raise HarnessError("MODEL_NOT_FOUND", "要修改的模型配置不存在", 404)
        if existing and existing.get("adapter_id") == "demo":
            raise HarnessError("MODEL_DEMO_READONLY", "本地演示配置不能替换为云端模型", 422)
        raw_endpoint = body.get("base_url") or preset["default_base_url"]
        endpoint = normalize_base_url(raw_endpoint)
        if not preset["allow_custom_base_url"] and endpoint != preset["default_base_url"]:
            raise HarnessError(
                "MODEL_PROVIDER_ENDPOINT", "预置提供方使用固定官方地址；其他地址请选择自定义", 422
            )
        identity = identity or ("model-" + new_id())
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", identity):
            raise HarnessError("MODEL_PROFILE_INVALID", "模型配置标识无效", 422)
        if saving and existing and body.get("expected_revision") != existing["revision"]:
            raise HarnessError("REVISION_CONFLICT", "模型配置已更新，请刷新后重试")
        if saving and not existing and body.get("expected_revision") not in {None, 0}:
            raise HarnessError("REVISION_CONFLICT", "新模型配置没有现有版本")
        model = str(body.get("model_id") or "").strip()
        if need_model and (not model or len(model) > 200 or any(ord(c) < 32 for c in model)):
            raise HarnessError("MODEL_NAME_REQUIRED", "请输入模型名称", 422)
        secret = body.get("api_key")
        if hasattr(secret, "get_secret_value"):
            secret = secret.get_secret_value()
        if secret is not None:
            secret = str(secret).strip()
        if secret and (len(secret) > 32768 or any(ord(c) < 32 for c in secret)):
            raise HarnessError("MODEL_KEY_INVALID", "API Key 格式无效", 422)
        if preset["id"] == "ollama":
            secret = None  # Switching to the keyless local provider never reuses or sends an old key.
        credential_ref = None
        if not secret and existing and existing.get("credential_ref") and preset["id"] != "ollama":
            previous_endpoint = normalize_base_url(
                self.services.resolve_endpoint(existing["endpoint_ref"], None)
            )
            if existing["provider_id"] != preset["id"] or previous_endpoint != endpoint:
                raise HarnessError("MODEL_KEY_REQUIRED", "切换提供方或地址时必须重新输入 API Key", 422)
            credential_ref = existing["credential_ref"]
            secret = self.services.vault.get(credential_ref, "model-" + identity)
        if preset["requires_api_key"] and not secret:
            raise HarnessError("MODEL_KEY_REQUIRED", "请输入此提供方的 API Key", 422)
        profile = ModelProfile(
            profile_id=identity,
            revision=(existing["revision"] if existing else 0) + 1,
            provider_id=preset["id"],
            adapter_id=preset["adapter"],
            api_mode=preset["api_mode"],
            endpoint_ref=endpoint,
            credential_ref="temporary:model-setup" if secret else None,
            model_id=model or "discovery-only",
            # Application context tier. The 1M choice is explicit configuration, not
            # a claim that the provider probe exercised one million tokens.
            limits=configured_model_limits(body.get("context_window", 300_000)),
        )
        # Text + tool request + tool result consume three calls. Each selected
        # optional probe adds one; the old fixed four-call cap silently skipped
        # the second optional capability before it reached the provider.
        probe_calls = (1 if body.get("probe_mode") == "connectivity" else 3) + len(
            setup_options(body)["optional_checks"]
        )
        ctx = self.services.diagnostic(owner, "model-setup:" + profile.ref, model_calls=probe_calls)
        grant = ModelSetupGrant(owner, ctx.diagnostic_id, profile.ref, endpoint)
        self._authorize(ctx, profile, grant)
        return preset, profile, secret, credential_ref, ctx, grant, existing

    def _authorize(self, ctx, profile, grant):
        self.services.boundary(ctx)
        try:
            self.services.policy.endpoint(
                profile.endpoint_ref,
                ctx,
                purpose="model",
                configured=True,
                model_profile_ref=profile.ref,
                model_setup_grant=grant,
            )
        except OSError:
            raise HarnessError(
                "MODEL_CONNECTION_FAILED", "模型地址无法解析或连接，请检查地址和网络", 422
            ) from None

    @staticmethod
    def _safe_error(exc):
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "__cause__", None), "status_code", None
        )
        if status in {401, 403}:
            return HarnessError("MODEL_AUTH_FAILED", "提供方拒绝了 API Key 或模型访问权限", 422)
        if status == 404:
            return HarnessError("MODEL_NOT_AVAILABLE", "模型名称或兼容接口不可用", 422)
        if status == 429:
            return HarnessError("MODEL_PROVIDER_RATE_LIMIT", "提供方额度或请求速率受限", 429)
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return HarnessError("MODEL_CONNECTION_TIMEOUT", "连接测试超时，请检查服务地址和网络", 504)
        return HarnessError("MODEL_CONNECTION_FAILED", "未能完成模型连接与协议验证，请检查配置", 422)

    async def _probe(self, profile, secret, ctx, grant, probe_mode="agent", optional_checks=None):
        request_count = 0

        def event(kind, payload):
            nonlocal request_count
            if kind == "model.attempt.started":
                request_count += 1
            self.services.metrics.record(kind, payload)

        gateway = ModelGateway(
            endpoint_resolver=lambda ref, context: profile.endpoint_ref,
            credential_accessor=lambda ref, endpoint, context: secret,
            policy_check=lambda context, selected: self._authorize(context, selected, grant),
            boundary_check=self.services.boundary,
            reserve=self.services.model_budget.reserve,
            settle=self.services.model_budget.settle,
            event_sink=event,
            max_attempts=1,
        )
        gateway.register(profile)
        try:
            async with asyncio.timeout(45):
                started = time.monotonic()
                report = await gateway.verify(
                    profile.ref, "text" if probe_mode == "connectivity" else "tools", ctx
                )
                report["suite_ref"] = SETUP_SUITE
                report["unverified_capabilities"].append("streaming")
                report["check_failures"] = {}
                for optional in dict.fromkeys(optional_checks or []):
                    report["requested_checks"].append(optional)
                    from langchain_core.messages import HumanMessage

                    try:
                        async with asyncio.timeout(8):
                            handle = await gateway.resolve(profile.ref, ctx)
                            handle = handle.model_copy(update={"verification_probe": True})
                            if optional == "streaming":
                                turn = await gateway.call(
                                    handle, [HumanMessage(content="Reply READY")], stream=True
                                )
                                passed = bool(turn.message.content) and not turn.message.tool_calls
                            else:
                                import base64
                                from io import BytesIO

                                from PIL import Image

                                color = secrets.choice(["red", "blue", "green", "yellow"])
                                fixture = BytesIO()
                                Image.new("RGB", (64, 64), color).save(fixture, format="PNG")
                                answer = await handle.ainvoke(
                                    [
                                        HumanMessage(
                                            content=[
                                                {
                                                    "type": "text",
                                                    "text": "Name the single dominant color of this image in one English word.",
                                                },
                                                {
                                                    "type": "image",
                                                    "mime_type": "image/png",
                                                    "base64": base64.b64encode(fixture.getvalue()).decode(),
                                                },
                                            ]
                                        )
                                    ]
                                )
                                passed = isinstance(answer.content, str) and bool(
                                    re.search(r"\b" + color + r"\b", answer.content.lower())
                                )
                            report["checks"][optional] = passed
                            if passed:
                                report["unverified_capabilities"] = [
                                    c for c in report["unverified_capabilities"] if c != optional
                                ]
                            else:
                                report["check_failures"][optional] = "PROBE_RESPONSE_MISMATCH"
                    except Exception as exc:
                        report["checks"][optional] = False
                        report["check_failures"][optional] = (
                            "PROBE_TIMEOUT" if isinstance(exc, TimeoutError) else "PROBE_FAILED"
                        )
                report["duration_ms"] = (time.monotonic() - started) * 1000
                report["request_count"] = request_count
                self.services.metrics.record(
                    "model_setup.phase",
                    dict(
                        span_id=new_id(),
                        phase="network/probe",
                        status="completed",
                        duration_ms=report["duration_ms"],
                        request_count=request_count,
                    ),
                )
                return report
        finally:
            await gateway.aclose()

    async def discover(self, owner, body):
        async with self._admit(owner):
            preset, profile, secret, _, ctx, grant, _ = self._prepare(owner, body)
            if preset["discovery_path"] is None:
                return {
                    "items": [dict(item) for item in preset["models"]],
                    "source": "official_catalog",
                    "message": "提供方官方预置名称；未验证当前账号可用性，可直接输入其他模型名称。",
                }
            headers = {}
            if secret:
                if preset["adapter"] == "anthropic":
                    headers = {"x-api-key": secret, "anthropic-version": "2023-06-01"}
                else:
                    headers = {"Authorization": "Bearer " + secret}
            url = profile.endpoint_ref + preset["discovery_path"]
            items = []
            try:
                async with asyncio.timeout(15):
                    async with httpx.AsyncClient(
                        timeout=10, follow_redirects=False, trust_env=False
                    ) as client:
                        self._authorize(ctx, profile, grant)
                        async with client.stream("GET", url, headers=headers) as response:
                            if 300 <= response.status_code < 400:
                                raise HarnessError("MODEL_REDIRECT_DENIED", "模型目录接口重定向已拒绝", 422)
                            response.raise_for_status()
                            raw = bytearray()
                            async for chunk in response.aiter_bytes():
                                if len(raw) + len(chunk) > 1024 * 1024:
                                    raise HarnessError(
                                        "MODEL_CATALOG_TOO_LARGE", "提供方返回的模型目录超过限制", 422
                                    )
                                raw.extend(chunk)
                data = json.loads(raw)
                records = data.get("models", []) if preset["adapter"] == "ollama" else data.get("data", [])
                if not isinstance(records, list):
                    raise TypeError("invalid catalog")
                for item in records[:500]:
                    if not isinstance(item, dict):
                        continue
                    identity = (
                        item.get("model", item.get("name"))
                        if preset["adapter"] == "ollama"
                        else item.get("id")
                    )
                    if (
                        isinstance(identity, str)
                        and 0 < len(identity) <= 200
                        and not any(ord(c) < 32 for c in identity)
                    ):
                        name = item.get("display_name") or identity
                        if secret and (secret in identity or secret in str(name)):
                            continue
                        items.append({"id": identity, "name": str(name)[:200]})
                truncated = bool(data.get("has_more")) or len(records) > 500
                return {
                    "items": list({item["id"]: item for item in items}.values()),
                    "source": "provider",
                    "message": "已读取提供方目录；目录未全部返回，可直接输入模型名称。"
                    if truncated
                    else "已读取提供方返回的模型目录；保存前仍会验证模型能力。",
                }
            except HarnessError:
                raise
            except Exception as exc:  # noqa: BLE001 - remote exceptions may contain headers or response bodies
                raise self._safe_error(exc) from None

    async def test(self, owner, body):
        async with self._admit(owner):
            _, profile, secret, _, ctx, grant, _ = self._prepare(owner, body, need_model=True)
            if body.get("refresh_verification"):
                # A user-requested retry must not return the previous partial failure.
                self._cache.pop(self._fingerprint(owner, profile, secret, setup_options(body)), None)
            try:
                report, token, reused = await self._verified(
                    owner,
                    profile,
                    secret,
                    ctx,
                    grant,
                    body.get("verification_token"),
                    setup_options(body),
                )
            except HarnessError:
                raise
            except Exception as exc:  # noqa: BLE001 - return only fixed sanitized diagnostic errors
                raise self._safe_error(exc) from None
            checks = report["checks"]
            tools = bool(checks.get("tool_calling") and checks.get("tool_pairing"))
            return {
                "verification_token": token,
                "reused": reused,
                "expires_in": max(
                    0,
                    int(
                        self._cache[
                            self._fingerprint(
                                owner,
                                profile,
                                secret,
                                setup_options(body),
                            )
                        ]["expires"]
                        - time.monotonic()
                    ),
                ),
                "checks": checks,
                "probe_mode": setup_options(body)["probe_mode"],
                "optional_checks": setup_options(body)["optional_checks"],
                "unverified_capabilities": [name for name in SETUP_CAPABILITIES if not checks.get(name)],
                "check_failures": report.get("check_failures", {}),
                "duration_ms": report.get("duration_ms", 0),
                "request_count": 0 if reused else report.get("request_count", 0),
                "connected": bool(checks.get("transport") and checks.get("text")),
                "tool_calling": tools,
                "message": "连接及工具协议验证通过；未执行真实工具。"
                if tools
                else "连通性检查完成；工具能力尚未验证。"
                if body.get("probe_mode") == "connectivity"
                else "未通过本助手需要的文本与工具协议验证。",
            }

    async def save(self, owner, body):
        async with self._admit(owner):
            _, profile, secret, retained_ref, ctx, grant, existing = self._prepare(
                owner, body, need_model=True, saving=True
            )
            activate = body.get("activate", True)
            if activate and body.get("probe_mode") == "connectivity":
                raise HarnessError(
                    "MODEL_FULL_VERIFICATION_REQUIRED", "快速连接检测不能用于激活，请执行完整验证", 422
                )
            if (
                not activate
                and existing
                and any(c.get("status") == "verified" for c in existing.get("capabilities", {}).values())
            ):
                raise HarnessError(
                    "MODEL_VERIFIED_DRAFT",
                    "已验证模型不能被未验证草稿覆盖；请添加独立草稿或完整验证后保存",
                    422,
                )
            if activate:
                try:
                    report, token, reused = await self._verified(
                        owner,
                        profile,
                        secret,
                        ctx,
                        grant,
                        body.get("verification_token"),
                        setup_options(body),
                    )
                except HarnessError:
                    raise
                except Exception as exc:
                    raise self._safe_error(exc) from None
            else:
                report, reused = (
                    dict(
                        verification_ref=new_id(), checks={}, scope="unverified_draft", created_at=utc_now()
                    ),
                    False,
                )
            checks = report["checks"]
            if activate and not all(checks.get(key) for key in SETUP_REQUIRED):
                raise HarnessError(
                    "MODEL_TOOL_UNAVAILABLE", "模型未通过文本与多轮工具协议验证，默认模型未修改", 422
                )
            missing = [name for name in SETUP_CAPABILITIES if not checks.get(name)] if activate else []
            if missing and (
                not body.get("verification_token")
                or set(body.get("accept_unverified_capabilities") or []) != set(missing)
            ):
                raise HarnessError(
                    "MODEL_CAPABILITIES_INCOMPLETE",
                    "部分能力未通过验证，原配置未修改。请查看完整检测结果、重试，或明确选择仅启用已验证能力。",
                    422,
                )
            report = {**report, "accepted_unverified_capabilities": missing}
            self._maintenance()
            self._authorize(ctx, profile, grant)
            created_ref = None
            published = False
            try:
                if secret and not retained_ref:
                    try:
                        with self._phase("credential_write"):
                            created_ref = self.services.vault.put("model-" + profile.profile_id, secret)
                    except Exception:  # noqa: BLE001 - vault implementation errors must not reveal input
                        raise HarnessError(
                            "MODEL_CREDENTIAL_STORAGE_FAILED", "无法将 API Key 写入系统凭据库", 503
                        ) from None
                verified = profile.model_copy(
                    update={
                        "credential_ref": retained_ref or created_ref,
                        "verification_ref": report["verification_ref"],
                        "capabilities": {
                            name: Capability(status="verified", evidence_ref=report["verification_ref"])
                            for name in ("text", "tool_calling", "streaming", "vision")
                            if checks.get(name)
                        },
                    }
                )
                with self._phase("db_publish"), self.store.transaction():
                    self._maintenance()
                    expected = existing["revision"] if existing else None
                    current = self.store.get("config/models", profile.profile_id)
                    if (current["revision"] if current else None) != expected:
                        raise HarnessError("REVISION_CONFLICT", "模型配置已更新，请刷新后重试")
                    self.store.compare_and_set(
                        "config/models",
                        profile.profile_id,
                        expected,
                        dict(id=profile.profile_id, **verified.model_dump(mode="json")),
                    )
                    self.store.compare_and_set(
                        "model_profiles", verified.ref, None, verified.model_dump(mode="json")
                    )
                    self.store.put("model_verifications", report["verification_ref"], report)
                    self.store.put(
                        "model_endpoint_grants",
                        verified.ref,
                        {
                            "owner_id": owner,
                            "profile_ref": verified.ref,
                            "endpoint": verified.endpoint_ref,
                            "enabled": True,
                            "source": "paired_model_setup",
                            "created_at": utc_now(),
                        },
                    )
                    if activate:
                        agent = self.store.get("config/agents", "default")
                        updated = AgentSpec.model_validate(
                            {
                                **agent,
                                "revision": agent["revision"] + 1,
                                "model_policy": {
                                    **agent["model_policy"],
                                    "profile_ref": verified.ref,
                                    "summary_profile_ref": verified.ref,
                                },
                            }
                        )
                        self.store.compare_and_set(
                            "config/agents", "default", agent["revision"], updated.model_dump(mode="json")
                        )
                published = True
                self.services.models.profiles[verified.ref] = verified
                return {
                    "profile": verified.model_dump(mode="json"),
                    "id": verified.profile_id,
                    "revision": verified.revision,
                    "active": activate,
                    "verification_reused": reused,
                    "message": "已保存并设为后续任务默认模型；现有任务保留原配置。"
                    if activate
                    else "已保存未验证草稿；未激活。",
                }
            except BaseException:
                if created_ref and not published:
                    try:
                        self.services.vault.delete(created_ref, "model-" + profile.profile_id)
                    except Exception:  # noqa: BLE001, S110 - orphaned vault reference is not published or usable
                        pass
                raise
