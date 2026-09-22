"""The sole owner of model attempts, retry budget, and complete-turn validation."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import random
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

from harness.core import DiagnosticContext

from .profiles import (
    DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS,
    GatewayError,
    ModelProfile,
    TokenEstimate,
    Usage,
    demo_profile,
)
from .protocol import StreamAssembler, validate_history, validate_response
from .providers import build_provider_model


async def maybe_await(value):
    return await value if inspect.isawaitable(value) else value


def estimate_tokens(request: dict, profile: ModelProfile | None = None) -> TokenEstimate:
    """Conservative UTF-8 byte estimate, including schemas and protocol wrapping."""
    import math
    image_tokens = 0
    def sanitize(value):
        nonlocal image_tokens
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, dict):
            if value.get("type") in ("image", "image_url", "input_image"):
                width, height = value.get("width", 0), value.get("height", 0)
                if not width and value.get("base64"):
                    from harness.artifacts.images import validate_image
                    import base64
                    dims = validate_image(base64.b64decode(value["base64"], validate=True), value["mime_type"])
                    width, height = dims["width"], dims["height"]
                image_tokens += max(4096, math.ceil(width / 28) * math.ceil(height / 28))
                return {"type": "image", "estimate_source": "app:conservative-28px-patches-v1"}
            return {k: sanitize(v) for k, v in value.items()}
        return value
    categories = {}
    for key, value in request.items():
        raw = json.dumps(sanitize(value), ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        categories[key] = len(raw.encode("utf-8")) + 8
    categories["images"] = image_tokens
    margin = profile.token_estimator.safety_margin if profile else 0.2
    return TokenEstimate(total=sum(categories.values()), categories=categories,
                         algorithm_version="utf8-plus-image-patches-v2", error_margin=margin)


class GatewayModelHandle(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    inner: Any = Field(exclude=True)
    gateway: Any = Field(exclude=True)
    profile: ModelProfile
    execution_context: Any = Field(exclude=True)
    schemas: dict[str, dict] = Field(default_factory=dict)
    verification_probe: bool = Field(default=False, exclude=True)
    output_reserve: int | None = Field(default=None, exclude=True)
    call_binding: dict = Field(default_factory=dict, exclude=True)

    @property
    def _llm_type(self):
        return f"harness-gateway-{self.profile.adapter_id}"

    def invoke(self, input, config=None, *, stop=None, **kwargs):
        from langsmith import tracing_context

        # Suppress ambient tracing before BaseChatModel creates its callback manager.
        with tracing_context(enabled=False):
            return super().invoke(input, config=config, stop=stop, **kwargs)

    async def ainvoke(self, input, config=None, *, stop=None, **kwargs):
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            return await super().ainvoke(input, config=config, stop=stop, **kwargs)

    def stream(self, input, config=None, *, stop=None, **kwargs):
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            yield from super().stream(input, config=config, stop=stop, **kwargs)

    async def astream(self, input, config=None, *, stop=None, **kwargs):
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            async for chunk in super().astream(input, config=config, stop=stop, **kwargs):
                yield chunk

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tools and not self.profile.supports("tool_calling") and not self.verification_probe:
            raise GatewayError(
                "capability_unverified", "Native tool calling has not been verified for this profile revision"
            )
        schemas = {}
        for tool in tools:
            spec = convert_to_openai_tool(tool)["function"]
            schemas[spec["name"]] = spec.get("parameters", {"type": "object"})
        options = dict(kwargs)
        # create_agent passes model_settings to bind_tools. Host-only budgeting
        # must remain on this wrapper instead of becoming a provider wire field.
        output_reserve = options.pop("harness_output_reserve", self.output_reserve)
        if tool_choice is not None:
            options["tool_choice"] = tool_choice
        if self.profile.adapter_id == "openai":
            options["parallel_tool_calls"] = self.profile.supports("parallel_tools")
        bound = self.inner.bind_tools(tools, **options) if tools else self.inner
        return self.model_copy(
            update={"inner": bound, "schemas": schemas, "output_reserve": output_reserve}
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, stop, run_manager, **kwargs))
        raise GatewayError("async_required", "Use ainvoke from an asynchronous runtime")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        turn = await self.gateway.call(
            self,
            messages,
            stream=self.gateway.stream_output
            and (self.profile.supports("streaming") or self.profile.adapter_id == "demo"),
            stop=stop,
            **kwargs,
        )
        return ChatResult(generations=[ChatGeneration(message=turn.message)])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        # Complete response is deliberately buffered: partial tool arguments never leave the gateway.
        turn = await self.gateway.call(
            self,
            messages,
            stream=self.profile.supports("streaming") or self.profile.adapter_id == "demo",
            stop=stop,
            **kwargs,
        )
        data = turn.message.model_dump(exclude={"type"})
        yield ChatGenerationChunk(message=AIMessageChunk(**data))


class ModelGateway:
    def __init__(
        self,
        repository=None,
        *,
        endpoint_resolver=None,
        credential_accessor=None,
        policy_check=None,
        reserve=None,
        settle=None,
        boundary_check=None,
        event_sink=None,
        max_attempts=3,
        retry_delay=0.1,
        provider_factory=build_provider_model,
        stream_output=False,
    ):
        self.repository = repository
        self.endpoint_resolver = endpoint_resolver
        self.credential_accessor = credential_accessor
        self.policy_check = policy_check
        self.reserve = reserve
        self.settle = settle
        self.boundary_check = boundary_check
        self.event_sink = event_sink
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.provider_factory = provider_factory
        self.stream_output = stream_output
        self.profiles = {"demo@1": demo_profile()}
        self._models = []
        self._model_cache = {}

    def register(self, profile: ModelProfile):
        previous = self.profiles.get(profile.ref)
        if previous is not None and previous != profile:
            raise GatewayError("immutable_profile", "Editing a model profile requires a new revision")
        if self.repository:
            existing = self.repository.get("model_profiles", profile.ref)
            if existing and existing != profile.model_dump(mode="json"):
                raise GatewayError("immutable_profile", "Profile revision already exists")
            self.repository.put("model_profiles", profile.ref, profile.model_dump(mode="json"))
        self.profiles[profile.ref] = profile
        return profile

    def get_profile(self, ref) -> ModelProfile:
        if isinstance(ref, ModelProfile):
            return ref
        if isinstance(ref, dict):
            ref = f"{ref['profile_id']}@{ref['revision']}"
        if ref == "demo":
            ref = "demo@1"
        data = self.repository.get("model_profiles", ref) if self.repository else None
        if data:
            return ModelProfile.model_validate(data)
        if ref not in self.profiles:
            raise GatewayError("unknown_profile", "Model profile revision is not configured")
        return self.profiles[ref]

    async def resolve(self, profile_ref, execution_context) -> GatewayModelHandle:
        profile = self.get_profile(profile_ref)
        if self.policy_check:
            await maybe_await(self.policy_check(execution_context, profile))
        elif profile.adapter_id != "demo":
            raise GatewayError("missing_policy", "External provider requires an endpoint policy check")
        endpoint = (
            await maybe_await(self.endpoint_resolver(profile.endpoint_ref, execution_context))
            if self.endpoint_resolver
            else profile.endpoint_ref
        )
        secret = None
        if profile.credential_ref:
            if not self.credential_accessor:
                raise GatewayError("missing_credential", "Credential accessor is not configured")
            secret = await maybe_await(
                self.credential_accessor(profile.credential_ref, endpoint, execution_context)
            )
        cache_key = (profile.ref, endpoint, hashlib.sha256((secret or "").encode()).hexdigest())
        model = self._model_cache.get(cache_key)
        if model is None:
            model = self.provider_factory(profile, endpoint, secret)
            self._models.append(model)
            self._model_cache[cache_key] = model
        return GatewayModelHandle(
            inner=model, gateway=self, profile=profile, execution_context=execution_context
        )

    async def aclose(self):
        """Release provider-owned connection pools on Host shutdown."""
        seen = set()
        for model in self._models:
            for candidate in (
                getattr(model, "root_async_client", None),
                getattr(model, "_async_client", None),
            ):
                if candidate is None or id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                closer = getattr(candidate, "aclose", None) or getattr(candidate, "close", None)
                if closer is None:
                    closer = getattr(getattr(candidate, "_client", None), "aclose", None)
                if closer:
                    await maybe_await(closer())
            for candidate in (getattr(model, "root_client", None), getattr(model, "_client", None)):
                if candidate is None or id(candidate) in seen:
                    continue
                seen.add(id(candidate))
                closer = getattr(candidate, "close", None) or getattr(
                    getattr(candidate, "_client", None), "close", None
                )
                if closer:
                    await maybe_await(closer())
        self._models.clear()
        self._model_cache.clear()

    async def _emit(self, kind: str, data: dict):
        if self.event_sink:
            await maybe_await(self.event_sink(kind, data))

    async def call(self, handle: GatewayModelHandle, messages: list[BaseMessage], *, stream=False, **kwargs):
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            return await self._call(handle, messages, stream=stream, **kwargs)

    async def _call(self, handle: GatewayModelHandle, messages: list[BaseMessage], *, stream=False, **kwargs):
        validate_history(messages)
        output_reserve = (
            kwargs.pop("harness_output_reserve", None)
            or handle.output_reserve
            or handle.profile.limits.output_limit
            or 1024
        )
        if handle.profile.limits.output_limit:
            output_reserve = min(output_reserve, handle.profile.limits.output_limit)
        if handle.profile.adapter_id in {"openai", "anthropic"}:
            kwargs["max_tokens"] = output_reserve
        elif handle.profile.adapter_id == "ollama":
            kwargs["options"] = {**kwargs.get("options", {}), "num_predict": output_reserve}
        for message in messages:
            if (
                isinstance(message.content, list)
                and any(
                    isinstance(block, dict) and block.get("type") in {"image", "image_url", "input_image"}
                    for block in message.content
                )
                and not handle.profile.supports("vision")
                and not handle.verification_probe
            ):
                raise GatewayError("vision_unverified", "Vision is not verified for this model profile")
        last_error = None
        message_id = str(uuid4())
        for attempt_index in range(self.max_attempts):
            ctx = handle.execution_context
            if self.boundary_check:
                await maybe_await(self.boundary_check(ctx))
            if self.policy_check:
                await maybe_await(self.policy_check(ctx, handle.profile))
            deadline = getattr(ctx, "deadline_at", None)
            timeout = DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS
            if deadline:
                limit = datetime.fromisoformat(deadline) if isinstance(deadline, str) else deadline
                if datetime.now(UTC) >= limit:
                    raise GatewayError("deadline_exceeded", "Model deadline has expired")
                timeout = min(timeout, (limit - datetime.now(UTC)).total_seconds())
            attempt_id = str(uuid4())
            estimate = estimate_tokens(
                {"messages": [m.model_dump(mode="json") for m in messages], "tools": handle.schemas},
                handle.profile,
            )
            reservation = (
                await maybe_await(self.reserve(ctx, attempt_id, estimate.total, output_reserve))
                if self.reserve
                else None
            )
            started = time.monotonic()
            first_token_ms = None
            identity = {
                "run_id": getattr(ctx, "run_id", None),
                "diagnostic_id": getattr(ctx, "diagnostic_id", None),
                "trace_id": getattr(ctx, "trace_id", None),
                "model_attempt_id": attempt_id,
            }
            await self._emit(
                "model.attempt.started",
                {
                    **identity,
                    "profile_ref": handle.profile.ref,
                    "retry": attempt_index,
                    "reserved_input_tokens": estimate.total,
                    "reserved_output_tokens": output_reserve,
                },
            )
            usage = Usage()
            try:
                if stream:
                    assembler = StreamAssembler(attempt_id)
                    chunk_index = 0
                    iterator = handle.inner.astream(messages, **kwargs).__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                anext(iterator), max(0.001, timeout - (time.monotonic() - started))
                            )
                        except StopAsyncIteration:
                            break
                        if self.boundary_check:
                            await maybe_await(self.boundary_check(ctx))
                        assembler.push(attempt_id, chunk)
                        text = (
                            chunk.content
                            if isinstance(chunk.content, str)
                            else "".join(
                                block.get("text", "")
                                for block in chunk.content
                                if isinstance(block, dict) and block.get("type") == "text"
                            )
                        )
                        if text and first_token_ms is None:
                            first_token_ms = (time.monotonic() - started) * 1000
                        if text and getattr(ctx, "run_id", None):
                            await self._emit(
                                "message.delta",
                                {
                                    "run_id": ctx.run_id,
                                    "message_id": message_id,
                                    "stream_id": attempt_id,
                                    "model_attempt_id": attempt_id,
                                    "chunk_index": chunk_index,
                                    "text": text,
                                },
                            )
                            chunk_index += 1
                    metadata = assembler.chunk.response_metadata if assembler.chunk else {}
                    if not (
                        metadata.get("finish_reason") or metadata.get("stop_reason") or metadata.get("done")
                    ):
                        raise GatewayError("incomplete_stream", "Provider stream has no completion marker")
                    turn = assembler.finish(completed=True, schemas=handle.schemas)
                else:
                    response = await asyncio.wait_for(handle.inner.ainvoke(messages, **kwargs), timeout)
                    turn = validate_response(response, handle.schemas)
                if (
                    turn.message.tool_calls
                    and not handle.profile.supports("tool_calling")
                    and not handle.verification_probe
                ):
                    raise GatewayError("capability_unverified", "Provider returned unverified tool calls")
                # Multiple valid tool requests are not permission for concurrent execution.
                # Compatible providers may return a batch even when parallel_tool_calls=False.
                # Preserve every call/result pair and let the Host serialize unverified batches.
                usage = turn.usage
                provider_id = turn.message.id
                turn.message.id = message_id
                turn.message.response_metadata["provider_message_id"] = provider_id
                turn.message.response_metadata["harness_binding"] = {
                    **handle.call_binding, "profile_ref": handle.profile.ref, "model_attempt_id": attempt_id,
                    "tool_execution": "parallel" if handle.profile.supports("parallel_tools") else "serial",
                }
                if (
                    usage.source == "provider"
                    and handle.profile.input_price_per_million is not None
                    and handle.profile.output_price_per_million is not None
                ):
                    usage.cost = (
                        usage.input_tokens * handle.profile.input_price_per_million
                        + usage.output_tokens * handle.profile.output_price_per_million
                    ) / 1_000_000
                await self._emit(
                    "model.attempt.completed",
                    {
                        **identity,
                        "usage": usage.model_dump(),
                        "duration_ms": (time.monotonic() - started) * 1000,
                        "first_token_ms": first_token_ms,
                    },
                )
                return turn
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                retryable = (
                    status == 429
                    or (isinstance(status, int) and status >= 500)
                    or isinstance(exc, (ConnectionError, TimeoutError))
                )
                if isinstance(exc, GatewayError):
                    retryable = exc.retryable
                last_error = (
                    exc
                    if isinstance(exc, GatewayError)
                    else GatewayError(
                        "provider_error", f"Model request failed ({type(exc).__name__})", retryable=retryable
                    )
                )
                await self._emit(
                    "model.attempt.failed",
                    {
                        **identity,
                        "code": last_error.code,
                        "retryable": retryable,
                        "duration_ms": (time.monotonic() - started) * 1000,
                    },
                )
                if not retryable or attempt_index + 1 >= self.max_attempts:
                    raise last_error from exc
            finally:
                if self.settle:
                    await maybe_await(self.settle(ctx, reservation, usage.model_dump()))
            await asyncio.sleep(self.retry_delay * (2**attempt_index) + random.uniform(0, self.retry_delay))
        raise last_error

    async def verify(self, profile_ref, suite_ref, diagnostic_context):
        # Fixed, bounded fixtures. Tool calls are only inspected; no executor is available here.
        from langchain_core.messages import HumanMessage, ToolMessage

        if not isinstance(diagnostic_context, DiagnosticContext):
            raise GatewayError(
                "invalid_diagnostic_context", "Capability verification requires a Host diagnostic identity"
            )
        suites = {
            "tools": ["transport", "messages", "tool_calling", "usage"],
            "fixed-v1": ["transport", "messages", "tool_calling", "usage"],
            "protocol-v1": ["transport", "messages", "tool_calling", "streaming", "usage"],
            "text": ["transport", "messages"],
            "connectivity": ["transport"],
        }
        requested = suites.get(suite_ref, [suite_ref]) if isinstance(suite_ref, str) else list(suite_ref)
        if not requested or set(requested) - {
            "transport",
            "messages",
            "capabilities",
            "tool_calling",
            "streaming",
            "usage",
        }:
            raise GatewayError("unknown_verification_suite", "Unknown model verification checks")
        profile = self.get_profile(profile_ref)
        handle = await self.resolve(profile, diagnostic_context)
        report = {
            "verification_ref": str(uuid4()),
            "profile_ref": profile.ref,
            "adapter": profile.adapter_id,
            "endpoint_ref": profile.endpoint_ref,
            "model": profile.model_id,
            "api_mode": profile.api_mode,
            "suite_ref": suite_ref,
            "requested_checks": requested,
            "scope": "protocol_probe_only",
            "created_at": datetime.now(UTC).isoformat(),
            "checks": {},
            "unverified_capabilities": [
                "parallel_tools",
                "vision",
                "structured_output",
                "provider_continuation",
            ],
        }
        response = await handle.ainvoke([HumanMessage(content="Reply with the word READY.")])
        if "transport" in requested:
            report["checks"]["transport"] = True
        if "messages" in requested or "capabilities" in requested:
            report["checks"]["text"] = bool(response.content)
        if "usage" in requested:
            report["checks"]["usage"] = response.usage_metadata is not None
        if ("tool_calling" in requested or "capabilities" in requested) and (
            profile.capabilities.get("tool_calling", None) is None
            or profile.capabilities["tool_calling"].status != "unsupported"
        ):
            tool = {
                "type": "function",
                "function": {
                    "name": "fixture_lookup",
                    "description": "Read fixed fixture value",
                    "parameters": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}},
                        "required": ["key"],
                    },
                },
            }
            bound = handle.model_copy(update={"verification_probe": True}).bind_tools([tool])
            question = HumanMessage(
                content='/tool fixture_lookup {"key":"ready"}'
                if profile.adapter_id == "demo"
                else "Call fixture_lookup with key ready."
            )
            answer = await bound.ainvoke([question])
            report["checks"]["tool_calling"] = bool(answer.tool_calls)
            if answer.tool_calls:
                follow = await bound.ainvoke(
                    [question, answer]
                    + [ToolMessage(content="READY", tool_call_id=c["id"]) for c in answer.tool_calls]
                )
                report["checks"]["tool_pairing"] = bool(follow.content) and not follow.tool_calls
        if "streaming" in requested:
            streamed = await self.call(handle, [HumanMessage(content="Reply with READY.")], stream=True)
            report["checks"]["streaming"] = bool(streamed.message.content)
        if self.repository:
            self.repository.put("model_verifications", report["verification_ref"], report)
        return report

    async def structured_call(self, handle, messages, schema, *, repair_attempts=0):
        """Explicit bounded text-schema repair; every repair is another governed model call."""
        import jsonschema
        from langchain_core.messages import HumanMessage

        if not 0 <= repair_attempts <= 2:
            raise GatewayError("invalid_repair_limit", "Structured repair limit must be between zero and two")
        active = list(messages)
        for index in range(repair_attempts + 1):
            answer = await handle.ainvoke(active)
            try:
                value = json.loads(answer.content)
                jsonschema.Draft202012Validator(schema).validate(value)
                return value
            except (ValueError, TypeError, jsonschema.ValidationError) as exc:
                if index == repair_attempts:
                    raise GatewayError(
                        "invalid_structured_response", "Model response failed structured output validation"
                    ) from exc
                active.extend(
                    [
                        answer,
                        HumanMessage(
                            content="Return a complete JSON value matching this schema. No markdown. "
                            + json.dumps(schema, ensure_ascii=False)
                        ),
                    ]
                )

    async def switch(self, profile_ref, execution_context, messages, previous_profile, rebuild_context):
        from .protocol import check_model_switch

        target = self.get_profile(profile_ref)
        check_model_switch(messages, previous_profile, target)
        # Recompose even when limits appear larger: tools and estimator can change by adapter.
        context_view = await maybe_await(rebuild_context(target))
        return await self.resolve(target, execution_context), context_view
