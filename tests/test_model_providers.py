"""Real provider SDKs against isolated local protocol fixtures, not vendor certification."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from harness.core import DiagnosticContext
from harness.model_gateway import (
    Capability,
    GatewayError,
    ModelGateway,
    ModelLimits,
    ModelProfile,
    demo_profile,
)


@pytest.fixture
def provider_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            messages = body["messages"]
            tool_result = messages[-1].get("role") == "tool" or (
                isinstance(messages[-1].get("content"), list)
                and any(
                    b.get("type") == "tool_result" for b in messages[-1]["content"] if isinstance(b, dict)
                )
            )
            if self.path.endswith("/chat/completions"):
                message = {"role": "assistant", "content": "fixture complete" if tool_result else None}
                if not tool_result:
                    message["tool_calls"] = [
                        {
                            "id": "fixture-call",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"fixture"}'},
                        }
                    ]
                if self.path.startswith("/batch") and not tool_result:
                    message["tool_calls"].append(
                        {
                            "id": "fixture-call-two",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"fixture-two"}'},
                        }
                    )
                    if self.path.startswith("/batch-invalid"):
                        message["tool_calls"][1]["function"]["arguments"] = '{"text":42}'
                    if self.path.startswith("/batch-duplicate"):
                        message["tool_calls"][1]["id"] = "fixture-call"
                    if self.path.startswith("/batch-unknown"):
                        message["tool_calls"][1]["function"]["name"] = "unbound-tool"
                if self.path.startswith("/batch") and body.get("stream"):
                    delta = {"role": "assistant", "content": message["content"] or ""}
                    if message.get("tool_calls"):
                        delta["tool_calls"] = [
                            {"index": i, **call} for i, call in enumerate(message["tool_calls"])
                        ]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for piece, finish in ((delta, None), ({}, "stop" if tool_result else "tool_calls")):
                        chunk = dict(
                            id="fixture-response",
                            object="chat.completion.chunk",
                            created=1,
                            model=body["model"],
                            choices=[dict(index=0, delta=piece, finish_reason=finish)],
                        )
                        self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                response = {
                    "id": "fixture-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop" if tool_result else "tool_calls",
                            "message": message,
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                }
            elif self.path.endswith("/messages"):
                response = {
                    "id": "fixture-response",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": [{"type": "text", "text": "fixture complete"}]
                    if tool_result
                    else [
                        {
                            "type": "tool_use",
                            "id": "fixture-call",
                            "name": "echo",
                            "input": {"text": "fixture"},
                        }
                    ],
                    "stop_reason": "end_turn" if tool_result else "tool_use",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                }
            else:
                message = {"role": "assistant", "content": "fixture complete" if tool_result else ""}
                if not tool_result:
                    message["tool_calls"] = [{"function": {"name": "echo", "arguments": {"text": "fixture"}}}]
                response = {
                    "model": body["model"],
                    "created_at": "2026-09-17T00:00:00Z",
                    "message": message,
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 12,
                    "eval_count": 4,
                }
            raw = json.dumps(response).encode() + b"\n"
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/x-ndjson" if self.path.endswith("/api/chat") else "application/json",
            )
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize(
    "provider_id,adapter,mode,suffix",
    [
        ("openai", "openai", "chat_completions", "/v1"),
        ("qwen", "openai", "chat_completions", "/v1"),
        ("anthropic", "anthropic", "messages", ""),
        ("ollama", "ollama", "chat", ""),
    ],
)
@pytest.mark.asyncio
async def test_real_adapter_protocol_fixture_multi_round(
    provider_server, provider_id, adapter, mode, suffix
):
    from harness.model_gateway import DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS

    assert DEFAULT_MODEL_REQUEST_TIMEOUT_SECONDS == 180.0
    endpoint, calls = provider_server
    policy_checks, reservations, settlements = [], [], []
    profile = ModelProfile(
        profile_id=provider_id,
        provider_id=provider_id,
        adapter_id=adapter,
        endpoint_ref="fixture-endpoint",
        credential_ref="fixture-key" if adapter != "ollama" else None,
        model_id="qwen3.8-flash" if provider_id == "qwen" else "fixture-model",
        api_mode=mode,
        capabilities={
            name: Capability(status="verified", evidence_ref="local-protocol-fixture-only")
            for name in ("text", "tool_calling", "parallel_tools", "vision")
        },
        limits=ModelLimits(context_window=8192, output_limit=512, source_ref="local-fixture"),
    )
    gateway = ModelGateway(
        endpoint_resolver=lambda ref, ctx: endpoint + suffix,
        credential_accessor=lambda ref, endpoint, ctx: "fixture-not-a-real-secret",
        policy_check=lambda ctx, profile: policy_checks.append(profile.ref),
        reserve=lambda ctx, attempt, estimated, output: reservations.append(attempt) or attempt,
        settle=lambda ctx, reservation, usage: settlements.append(usage),
    )
    gateway.register(profile)
    handle = await gateway.resolve(profile.ref, SimpleNamespace(deadline_at=None))
    bound = handle.bind_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "fixture",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ]
    )
    from io import BytesIO
    from PIL import Image
    import base64

    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    image_data = base64.b64encode(png.getvalue()).decode()
    question = HumanMessage(
        content=[
            {"type": "text", "text": "Call echo with fixture"},
            {"type": "image", "base64": image_data, "mime_type": "image/png"},
        ]
    )
    response = await bound.ainvoke([question], harness_output_reserve=123)
    assert response.tool_calls[0]["args"] == {"text": "fixture"}
    final = await bound.ainvoke(
        [question, response, ToolMessage(content="fixture", tool_call_id=response.tool_calls[0]["id"])],
        harness_output_reserve=123,
    )
    assert final.content == "fixture complete" or final.content[0]["text"] == "fixture complete"
    assert len(calls) == 2 and len(set(reservations)) == 2
    assert len(policy_checks) == 3  # Resolve plus current policy revalidation before both requests.
    assert all(item["source"] == "provider" for item in settlements)
    if adapter == "ollama":
        assert calls[0][1]["options"]["num_predict"] == 123
        assert calls[0][1]["messages"][0]["images"] == [image_data]
    elif adapter == "openai":
        assert calls[0][1].get("max_completion_tokens", calls[0][1].get("max_tokens")) == 123
        assert calls[0][1].get("enable_thinking") is (False if provider_id == "qwen" else None)
        image_block = calls[0][1]["messages"][0]["content"][1]
        assert image_block["image_url"]["url"] == "data:image/png;base64," + image_data
    else:
        assert calls[0][1]["max_tokens"] == 123
        assert calls[0][1]["messages"][0]["content"][1]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": image_data,
        }
    await gateway.aclose()


@pytest.mark.asyncio
async def test_gateway_retry_owner_unknown_usage_and_cancellation():
    class RetryModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("fixture disconnect")
            return AIMessage(content="complete", response_metadata={"finish_reason": "stop"})

    model = RetryModel()
    attempts, settlements = [], []
    gateway = ModelGateway(
        provider_factory=lambda *args: model,
        retry_delay=0,
        reserve=lambda ctx, attempt, *args: attempts.append(attempt) or attempt,
        settle=lambda ctx, reservation, usage: settlements.append(usage),
    )
    handle = await gateway.resolve("demo@1", SimpleNamespace(deadline_at=None))
    result = await handle.ainvoke([HumanMessage(content="hello")])
    assert result.content == "complete"
    assert model.calls == 2 and len(set(attempts)) == 2
    assert all(u["source"] == "unknown" and u["cost"] is None for u in settlements)

    def cancelled(ctx):
        raise GatewayError("cancelled", "cancelled")

    gateway.boundary_check = cancelled
    with pytest.raises(GatewayError):
        await handle.ainvoke([HumanMessage(content="do not send")])
    assert model.calls == 2


@pytest.mark.asyncio
async def test_unverified_tool_capability_is_text_only_and_diagnostics_are_bounded():
    gateway = ModelGateway()
    unverified = demo_profile().model_copy(
        update={
            "profile_id": "unverified",
            "capabilities": {
                "text": Capability(status="unverified"),
                "tool_calling": Capability(status="unverified"),
            },
        }
    )
    gateway.register(unverified)
    handle = await gateway.resolve(unverified.ref, SimpleNamespace(deadline_at=None))
    with pytest.raises(GatewayError):
        handle.bind_tools([{"name": "echo", "description": "echo", "parameters": {"type": "object"}}])
    diagnostic = DiagnosticContext(
        diagnostic_id="diagnostic",
        owner_id="owner",
        workspace_id=None,
        config_snapshot_id="snapshot",
        budget_account_id="account",
        deadline_at="2099-01-01T00:00:00Z",
        trace_id="trace",
    )
    report = await gateway.verify(unverified.ref, "fixed-v1", diagnostic)
    assert report["scope"] == "protocol_probe_only" and all(report["checks"].values())
    assert not gateway.get_profile(unverified.ref).supports("tool_calling")


@pytest.mark.asyncio
async def test_gateway_stream_rejects_missing_finish_and_does_not_leak_fragments():
    from langchain_core.messages import AIMessageChunk

    class PartialModel:
        async def astream(self, messages, **kwargs):
            yield AIMessageChunk(
                content="transient",
                tool_call_chunks=[{"name": "echo", "args": '{"text":"unfinished"', "id": "call", "index": 0}],
            )

    gateway = ModelGateway(provider_factory=lambda *args: PartialModel())
    handle = await gateway.resolve("demo@1", SimpleNamespace(deadline_at=None))
    chunks = []
    with pytest.raises(GatewayError):
        async for chunk in handle.astream([HumanMessage(content="fixture")]):
            chunks.append(chunk)
    assert chunks == []


@pytest.mark.asyncio
async def test_model_handle_does_not_inherit_ambient_remote_tracing(monkeypatch):
    import asyncio

    import langchain_core.tracers.langchain as tracing
    from langsmith import tracing_context

    attempted_exports = []

    class CanaryTracer(tracing.LangChainTracer):
        def __init__(self, *args, **kwargs):
            attempted_exports.append(True)
            raise AssertionError("Ambient telemetry must not instantiate a remote tracer")

    monkeypatch.setattr(tracing, "LangChainTracer", CanaryTracer)
    gateway = ModelGateway()
    handle = await gateway.resolve("demo@1", SimpleNamespace(deadline_at=None))
    with tracing_context(enabled=True):
        assert (await handle.ainvoke("fixture")).content
        assert [chunk async for chunk in handle.astream("fixture")]
        assert (await asyncio.to_thread(handle.invoke, "fixture")).content
    assert attempted_exports == []
