"""A provider ignoring parallel_tool_calls=False must retain all validated call/result pairs."""

import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness.context import ContextService
from harness.model_gateway import (
    Capability,
    GatewayError,
    ModelGateway,
    ModelLimits,
    ModelProfile,
    validate_history,
)
from harness.runtime import LangChainAgentRuntime
from harness.storage import Store
from test_model_providers import provider_server as _provider_server
from test_runtime_context_gateway import LedgerTool, context, snapshot


@pytest.fixture
def provider_server():
    yield from _provider_server.__wrapped__()


def profile(endpoint, *, parallel=False):
    capabilities = {
        name: Capability(status="verified", evidence_ref="fixture")
        for name in ("text", "tool_calling", "vision", "streaming")
    }
    if parallel:
        capabilities["parallel_tools"] = Capability(status="verified", evidence_ref="fixture")
    return ModelProfile(
        profile_id="batch",
        provider_id="custom",
        adapter_id="openai",
        endpoint_ref=endpoint,
        model_id="fixture-model",
        api_mode="chat_completions",
        capabilities=capabilities,
        limits=ModelLimits(input_limit=32768, output_limit=512),
    )


def image_message():
    raw = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(raw, format="PNG")
    return HumanMessage(
        id="u",
        content=[
            {"type": "text", "text": "Describe the image using both tools."},
            {"type": "image", "mime_type": "image/png", "base64": base64.b64encode(raw.getvalue()).decode()},
        ],
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_batch_preserves_all_calls_and_pairs_when_parallel_unverified(provider_server, stream):
    endpoint, calls = provider_server
    p = profile(endpoint + "/batch")
    gateway = ModelGateway(stream_output=stream, policy_check=lambda ctx, profile: None)
    gateway.register(p)
    try:
        handle = await gateway.resolve(p.ref, SimpleNamespace(deadline_at=None))
        bound = handle.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "fixture",
                        "parameters": snapshot()["tools"][0]["input_schema"],
                    },
                }
            ]
        )
        question = image_message()
        response = await bound.ainvoke([question])
        assert len(response.tool_calls) == 2 and not p.supports("parallel_tools")
        assert response.response_metadata["harness_binding"]["tool_execution"] == "serial"
        assert calls[0][1]["parallel_tool_calls"] is False
        history = [question, response] + [
            ToolMessage(content="fixture", tool_call_id=c["id"]) for c in response.tool_calls
        ]
        validate_history(history)
        final = await bound.ainvoke(history)
        assert final.content == "fixture complete"
        assert len([m for m in calls[-1][1]["messages"] if m["role"] == "tool"]) == 2
        assert len(calls) == 2  # No silent model retry or dropped tool request.
    finally:
        await gateway.aclose()


@pytest.mark.parametrize(
    "suffix,code",
    [("invalid", "invalid_tool_arguments"), ("duplicate", "invalid_tool_id"), ("unknown", "unknown_tool")],
)
@pytest.mark.parametrize("stream", [False, True])
async def test_invalid_batch_is_still_rejected_as_a_whole(provider_server, suffix, code, stream):
    endpoint, calls = provider_server
    p = profile(endpoint + "/batch-" + suffix)
    gateway = ModelGateway(stream_output=stream, policy_check=lambda ctx, profile: None)
    gateway.register(p)
    try:
        handle = await gateway.resolve(p.ref, SimpleNamespace(deadline_at=None))
        bound = handle.bind_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "fixture",
                        "parameters": snapshot()["tools"][0]["input_schema"],
                    },
                }
            ]
        )
        with pytest.raises(GatewayError) as error:
            await bound.ainvoke([image_message()])
        assert error.value.code == code and len(calls) == 1
    finally:
        await gateway.aclose()


class ObservedLedger(LedgerTool):
    def __init__(self, store, **kwargs):
        super().__init__(store, **kwargs)
        self.active = self.maximum = 0

    async def execute(self, *args):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.03)  # Expose overlapping ToolNode coroutines.
            return await super().execute(*args)
        finally:
            self.active -= 1


@pytest.mark.parametrize("parallel", [False, True])
async def test_real_graph_serializes_unverified_image_tool_batch_and_preserves_verified_parallelism(
    tmp_path, provider_server, parallel
):
    endpoint, calls = provider_server
    store = Store(tmp_path / "state.db")
    gateway = ModelGateway(store, stream_output=True, policy_check=lambda ctx, profile: None)
    p = profile(endpoint + "/batch", parallel=parallel)
    gateway.register(p)
    tool = ObservedLedger(store)
    spec = snapshot()
    spec["agent_spec"]["model_policy"]["profile_ref"] = p.ref
    try:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            agent = LangChainAgentRuntime(
                saver, gateway, ContextService(store, model_gateway=gateway), tool, repository=store
            )
            result = await agent.start(context(), spec, [image_message()])
            assert result.kind == "final", result.error
            assert tool.executions == 2 and tool.maximum == (2 if parallel else 1)
            state = (await saver.aget_tuple({"configurable": {"thread_id": "thread"}})).checkpoint[
                "channel_values"
            ]
            validate_history(state["messages"])
            assert len([m for m in state["messages"] if isinstance(m, ToolMessage)]) == 2
            assert len(calls) == 2
    finally:
        await gateway.aclose()


async def test_serial_batch_approval_resume_keeps_ids_and_does_not_repeat_effects(tmp_path, provider_server):
    endpoint, calls = provider_server
    store = Store(tmp_path / "state.db")
    gateway = ModelGateway(store, stream_output=True, policy_check=lambda ctx, profile: None)
    p = profile(endpoint + "/batch")
    gateway.register(p)
    tool = ObservedLedger(store, wait=True)
    spec = snapshot()
    spec["agent_spec"]["model_policy"]["profile_ref"] = p.ref
    path = str(tmp_path / "checkpoints.db")

    def runtime(saver):
        return LangChainAgentRuntime(
            saver, gateway, ContextService(store, model_gateway=gateway), tool, repository=store
        )

    try:
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            waiting = await runtime(saver).start(context(), spec, [image_message()])
            assert waiting.kind == "waiting", waiting.error
            assert tool.executions == 0 and tool.maximum == 1
            state = (await saver.aget_tuple({"configurable": {"thread_id": "thread"}})).checkpoint[
                "channel_values"
            ]
            assistant = next(m for m in state["messages"] if isinstance(m, AIMessage))
            assert assistant.response_metadata["harness_binding"]["tool_execution"] == "serial"
        tool.approved = True
        async with AsyncSqliteSaver.from_conn_string(path) as saver:
            result = await runtime(saver).resume(
                context(),
                waiting.checkpoint_ref,
                {
                    ref.interrupt_id: {"outcome": "answered", "reason": "fixture"}
                    for ref in waiting.interrupt_refs
                },
            )
            assert result.kind == "final", result.error
            assert tool.executions == 2 and tool.maximum == 1
            assert {key.tool_call_id for key in tool.keys} == {c["id"] for c in assistant.tool_calls}
            assert len(calls) == 2
    finally:
        await gateway.aclose()
