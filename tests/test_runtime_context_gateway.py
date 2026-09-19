"""Cross-boundary recovery tests use SQLite and the actual locked LangChain graph."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness.context import ContextPolicy, ContextService, MemoryService, SourceRevision, input_budget
from harness.core import ExecutionContext, HarnessError
from harness.model_gateway import (
    GatewayError,
    ModelGateway,
    StreamAssembler,
    demo_profile,
    validate_history,
)
from harness.runtime import AgentSpec, LangChainAgentRuntime
from harness.runtime.engine import model_tool_result
from harness.storage import Store


def context(**kwargs):
    return ExecutionContext(
        owner_id="owner",
        workspace_id="workspace",
        session_id="session",
        branch_id="branch",
        run_id="run",
        root_run_id="run",
        worker_attempt_id="attempt",
        fencing_token=1,
        graph_thread_key="thread",
        input_revision=1,
        config_snapshot_id="snapshot",
        trace_id="trace",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_context_policy_is_frozen_per_run_and_does_not_mutate_shared_service(tmp_path):
    store = Store(tmp_path / "state.db")
    gateway = ModelGateway(store)
    composer = ContextService(store, model_gateway=gateway, policy={"output_reserve": 1024})
    store.put(
        "snapshots",
        "snapshot",
        {"context_policy": ContextPolicy(output_reserve=128, safety_tokens=32).model_dump()},
    )
    first = await composer.compose(context(), [HumanMessage(id="u", content="hello")], demo_profile(), [])
    assert first.budget_breakdown["output_reserve"] == 128
    second_ctx = context().model_copy(
        update={
            "config_snapshot_id": "other-snapshot",
            "graph_thread_key": "second",
            "branch_id": "second",
            "run_id": "second",
        }
    )
    store.put(
        "snapshots",
        "other-snapshot",
        {"context_policy": ContextPolicy(output_reserve=256, safety_tokens=64).model_dump()},
    )
    second = await composer.compose(second_ctx, [HumanMessage(id="u2", content="hello")], demo_profile(), [])
    assert second.budget_breakdown["output_reserve"] == 256
    assert composer.policy.output_reserve == 1024


class LedgerTool:
    def __init__(self, store, *, wait=False):
        self.store = store
        self.executions = 0
        self.wait = wait
        self.approved = False
        self.keys = []

    async def execute(self, ctx, key, name, args):
        self.keys.append(key)
        saved = self.store.get("fixture_ledger", key.tool_call_id)
        if saved:
            return saved
        if self.wait and not self.approved:
            return {"kind": "user", "interaction_id": "fixture-interaction"}
        self.executions += 1
        result = {"status": "succeeded", "value": args["text"]}
        self.store.put("fixture_ledger", key.tool_call_id, result)
        return result


def snapshot():
    return {
        "agent_spec": AgentSpec(tools=["echo"]).model_dump(),
        "tools": [
            {
                "name": "echo",
                "description": "Echo text fixture",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        ],
    }


def runtime(store, saver, tool, **options):
    gateway = ModelGateway(store)
    composer = ContextService(store, model_gateway=gateway)
    return LangChainAgentRuntime(saver, gateway, composer, tool, repository=store, **options)


@pytest.mark.asyncio
async def test_real_create_agent_tool_loop_and_persistent_checkpoint(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    tool = LedgerTool(store)
    seen = []
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        agent = runtime(store, saver, tool, fault_hook=lambda point, ctx: seen.append(point))
        result = await agent.start(context(), snapshot(), '/tool echo {"text":"hello"}')
        assert result.kind == "final", result.error
        assert tool.executions == 1
        assert result.checkpoint_ref.checkpoint_id
        assert "hello" in store.get("runtime_results", "run")["content"]
        assert (
            seen.index("model_validated_before_checkpoint")
            < seen.index("model_checkpoint_before_tool")
            < seen.index("tool_result_before_checkpoint")
        )
        assert len(store.list("context_views")) == 2


@pytest.mark.asyncio
async def test_interrupt_persists_and_resumes_after_new_runtime(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    tool = LedgerTool(store, wait=True)
    ctx = context()
    path = str(tmp_path / "checkpoints.sqlite")
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        agent = runtime(store, saver, tool)
        waiting = await agent.start(ctx, snapshot(), '/tool echo {"text":"approved"}')
        assert waiting.kind == "waiting", waiting.error
        assert tool.executions == 0
        assert len(waiting.interrupt_refs) == 1
    tool.approved = True
    async with AsyncSqliteSaver.from_conn_string(path) as saver:
        agent = runtime(store, saver, tool)
        interrupted = waiting.interrupt_refs[0]
        result = await agent.resume(
            ctx,
            waiting.checkpoint_ref,
            {interrupted.interrupt_id: {"outcome": "answered", "reason": "fixture approval"}},
        )
        assert result.kind == "final", result.error
        assert tool.executions == 1
        assert len({key.model_dump_json() for key in tool.keys}) == 1


@pytest.mark.asyncio
async def test_model_checkpoint_before_tool_recovery_keeps_call_id(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    tool = LedgerTool(store)

    def crash(point, ctx):
        if point == "model_checkpoint_before_tool":
            raise RuntimeError("injected before tool")

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        agent = runtime(store, saver, tool, fault_hook=crash)
        failed = await agent.start(context(), snapshot(), '/tool echo {"text":"once"}')
        assert failed.kind == "error"
        assert tool.executions == 0
        record = await saver.aget_tuple({"configurable": {"thread_id": "thread"}})
        assistant = next(
            m for m in reversed(record.checkpoint["channel_values"]["messages"]) if isinstance(m, AIMessage)
        )
        expected_id = assistant.tool_calls[0]["id"]
        recovered = runtime(store, saver, tool)
        final = await recovered.resume(context(), failed.checkpoint_ref)
        assert final.kind == "final", final.error
        assert tool.executions == 1
        assert tool.keys[0].tool_call_id == expected_id


@pytest.mark.asyncio
async def test_ledger_saved_before_checkpoint_recovers_without_second_effect(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    tool = LedgerTool(store)

    def crash(point, ctx):
        if point == "tool_result_before_checkpoint":
            raise RuntimeError("injected after effect")

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        agent = runtime(store, saver, tool, fault_hook=crash)
        failed = await agent.start(context(), snapshot(), '/tool echo {"text":"once"}')
        assert failed.kind == "error"
        assert tool.executions == 1
        final = await runtime(store, saver, tool).resume(context(), failed.checkpoint_ref)
        assert final.kind == "final", final.error
        assert tool.executions == 1


@pytest.mark.asyncio
async def test_failure_before_model_checkpoint_has_no_tool_effect(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    tool = LedgerTool(store)

    def crash(point, ctx):
        if point == "model_validated_before_checkpoint":
            raise RuntimeError("injected before persistence")

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
        outcome = await runtime(store, saver, tool, fault_hook=crash).start(
            context(), snapshot(), '/tool echo {"text":"safe"}'
        )
        assert outcome.kind == "error"
        assert tool.executions == 0
        saved = await saver.aget_tuple({"configurable": {"thread_id": "thread"}})
        assert not any(isinstance(m, AIMessage) for m in saved.checkpoint["channel_values"]["messages"])


def test_stream_assembler_never_accepts_partial_or_invalid_calls():
    schema = {"echo": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
    stream = StreamAssembler("one")
    stream.push(
        "one",
        AIMessageChunk(
            content="", tool_call_chunks=[{"name": "echo", "args": '{"text":', "id": "call", "index": 0}]
        ),
    )
    with pytest.raises(GatewayError):
        stream.finish(completed=True, schemas=schema)
    stream = StreamAssembler("two")
    with pytest.raises(GatewayError):
        stream.push("wrong", AIMessageChunk(content="x"))
    stream.push(
        "two",
        AIMessageChunk(
            content="", tool_call_chunks=[{"name": "echo", "args": '{"text":', "id": "call", "index": 0}]
        ),
    )
    stream.push(
        "two",
        AIMessageChunk(
            content="", tool_call_chunks=[{"name": None, "args": '"ok"}', "id": None, "index": 0}]
        ),
    )
    turn = stream.finish(completed=True, schemas=schema)
    assert turn.message.tool_calls[0]["args"] == {"text": "ok"}
    assert turn.usage.source == "unknown" and turn.usage.cost is None


def test_history_and_budget_contracts():
    call = AIMessage(content="", tool_calls=[{"name": "echo", "args": {}, "id": "call"}])
    with pytest.raises(GatewayError):
        validate_history([call, HumanMessage(content="oops")])
    validate_history([call, ToolMessage(content="ok", tool_call_id="call")])
    profile = demo_profile().model_copy(
        update={
            "limits": demo_profile().limits.model_copy(update={"context_window": 10000, "input_limit": 9000})
        }
    )
    assert (
        input_budget(profile, ContextPolicy(output_reserve=1000, safety_tokens=100))["input_budget"] == 8900
    )


@pytest.mark.asyncio
async def test_context_hard_limit_and_immutable_sources(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    composer = ContextService(store)
    message = HumanMessage(content="hello", id="message")
    view = await composer.compose(context(), [message], demo_profile(), [])
    assert composer.materialize(context(), view)["messages"][0].content == "hello"
    with pytest.raises(HarnessError, match="archived"):
        await composer.compose(
            context(), [HumanMessage(content="modified", id="message")], demo_profile(), []
        )
    with pytest.raises(HarnessError, match="exceeding"):
        await composer.compose(
            context(), [HumanMessage(content="汉" * 120000, id="large")], demo_profile(), []
        )


@pytest.mark.asyncio
async def test_compaction_cas_cannot_overwrite_new_input(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    ctx = context()

    async def engine(ctx, messages, constraints):
        pointer = store.get("context_pointer", ctx.graph_thread_key)
        store.compare_and_set(
            "context_pointer",
            ctx.graph_thread_key,
            pointer["revision"],
            {**pointer, "revision": pointer["revision"] + 1, "input_revision": 2},
        )
        return {"goal": "continue", "constraints": constraints}

    composer = ContextService(store, summary_engine=engine)
    await composer.compose(
        ctx, [HumanMessage(content="task", id="one"), AIMessage(content="done", id="two")], demo_profile(), []
    )
    with pytest.raises(HarnessError):
        await composer.compact(
            ctx,
            SourceRevision(input_revision=1, context_revision=1),
            ["one", "two"],
            hard_constraints=["preserve files"],
        )
    assert store.get("context_pointer", "thread")["summary_ref"] is None
    assert store.get("context_pointer", "thread")["input_revision"] == 2
    assert len(store.list("context_archive")) == 2


def test_memory_review_isolation_cas_delete(tmp_path):
    service = MemoryService(Store(tmp_path / "business.sqlite"))
    record = service.propose("owner", "workspace", "prefer Python", ["source"])
    assert service.retrieve("owner", "workspace", "Python") == []
    accepted = service.review("owner", record.id, "accept", 1)
    assert service.retrieve("owner", "workspace", "Python")[0].id == record.id
    assert service.retrieve("owner", "different", "Python") == []
    assert service.retrieve("stranger", "workspace", "Python") == []
    with pytest.raises(HarnessError):
        service.update("owner", record.id, {"content": "new"}, 1)
    with pytest.raises(HarnessError):
        service.update("owner", record.id, {"owner": "stranger"}, accepted.revision)
    service.delete("owner", record.id, accepted.revision)
    assert service.retrieve("owner", "workspace", "Python") == []
    expired_record = service.propose(
        "owner", "workspace", "Python expired", [], expires_at="2000-01-01T08:00:00+08:00"
    )
    service.review("owner", expired_record.id, "accepted", expired_record.revision)
    assert service.retrieve("owner", "workspace", "Python") == []
    with pytest.raises(ValueError, match="timezone"):
        service.propose("owner", "workspace", "Python invalid", [], expires_at="2099-01-01T00:00:00")


@pytest.mark.asyncio
async def test_production_summary_profile_and_pin_revision_are_in_context(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    gateway = ModelGateway(store)
    store.put(
        "snapshots",
        "snapshot",
        {
            "agent_spec": {"model_policy": {"profile_ref": "demo@1"}},
            "model_profile": demo_profile().model_dump(mode="json"),
        },
    )
    store.put(
        "context_pins",
        "branch",
        {"revision": 1, "items": [{"id": "pin", "text": "Never delete source files", "source_ref": "user"}]},
    )
    composer = ContextService(
        store, model_gateway=gateway, policy={"soft_threshold": 0.01, "recent_turns": 1}
    )
    messages = [
        HumanMessage(content="old task " * 150, id="u1"),
        AIMessage(content="old evidence " * 150, id="a1"),
        HumanMessage(content="latest correction", id="u2"),
    ]
    view = await composer.compose(context(), messages, demo_profile(), [])
    assert view.pin_refs == ["pin"]
    assert view.source_revision.pin_revision == 1
    assert len(view.summary_refs) == 1
    summary = store.get("context_summaries", view.summary_refs[0])
    assert summary["model_profile_ref"] == "demo@1"
    assert summary["constraints"] == ["Never delete source files"]
    assert composer.materialize(context(), view)["messages"][-1].content == "latest correction"


def test_model_tool_result_projection_keeps_ledger_fields_out_of_agent_context():
    durable = {
        "operation_id": "operation",
        "status": "succeeded",
        "summary": "找到 1 条匹配；扫描完成",
        "structured_data": {"matches": [{"path": "one.py", "line": 1}], "complete": True},
        "content_blocks": [],
        "artifact_refs": [],
        "truncated": False,
        "exit_code": None,
        "started_at": "2026-09-19T00:00:00Z",
        "completed_at": "2026-09-19T00:00:01Z",
        "error_code": None,
        "retryable": False,
    }

    assert model_tool_result(durable) == {
        "status": "succeeded",
        "summary": "找到 1 条匹配；扫描完成",
        "structured_data": {"matches": [{"path": "one.py", "line": 1}], "complete": True},
        "truncated": False,
    }
    assert durable["operation_id"] == "operation"

    failed = {**durable, "status": "failed", "error_code": "FILE_NOT_FOUND"}
    assert model_tool_result(failed)["retryable"] is False
    assert model_tool_result(failed)["error_code"] == "FILE_NOT_FOUND"
    assert model_tool_result({"status": "succeeded", "value": "business result"}) == {
        "status": "succeeded",
        "value": "business result",
    }


@pytest.mark.asyncio
async def test_pin_change_during_summary_does_not_activate_old_summary(tmp_path):
    store = Store(tmp_path / "business.sqlite")
    store.put(
        "context_pins",
        "branch",
        {"revision": 1, "items": [{"id": "pin1", "text": "old", "source_ref": "user"}]},
    )

    def summarizer(ctx, messages, constraints):
        store.compare_and_set(
            "context_pins",
            "branch",
            1,
            {"revision": 2, "items": [{"id": "pin2", "text": "new", "source_ref": "user"}]},
        )
        return {"goal": "fixture", "constraints": constraints}

    composer = ContextService(store, summary_engine=summarizer)
    await composer.compose(
        context(),
        [HumanMessage(content="task", id="u"), AIMessage(content="reply", id="a")],
        demo_profile(),
        [],
    )
    with pytest.raises(HarnessError, match="Pinned"):
        await composer.compact(
            context(), SourceRevision(input_revision=1, context_revision=1, pin_revision=1), ["u", "a"]
        )
    assert store.get("context_pointer", "thread")["summary_ref"] is None


@pytest.mark.asyncio
async def test_attachment_resolution_reads_bounded_text_and_verified_images(tmp_path):
    import base64
    import hashlib
    from io import BytesIO

    from PIL import Image

    from harness.core import ArtifactRef
    from harness.model_gateway import Capability
    png = BytesIO()
    Image.new("RGB", (24, 24), "red").save(png, format="PNG")
    payloads = {"text-file": ("真实附件内容。" * 100).encode(), "image-file": png.getvalue()}
    reads = []

    def ref(identity, mime):
        return ArtifactRef(
            artifact_id=identity,
            content_hash=hashlib.sha256(payloads[identity]).hexdigest(),
            mime_type=mime,
            size_bytes=len(payloads[identity]),
            owner_id="owner",
            workspace_id="workspace",
            provenance_ref="upload",
        )

    def read(ctx, artifact, limit):
        reads.append((artifact.artifact_id, limit))
        return payloads[artifact.artifact_id][:limit]

    store = Store(tmp_path / "business.sqlite")
    composer = ContextService(store, artifact_reader=read, policy={"offload_bytes": 256})
    text_ref = ref("text-file", "text/plain")
    message = HumanMessage(
        id="text-u", content=[{"type": "file_reference", "artifact_ref": text_ref.model_dump()}]
    )
    view = await composer.compose(context(), [message], demo_profile(), [])
    actual = composer.materialize(context(), view)["messages"][-1].content[0]["text"]
    assert "真实附件内容" in actual and '"truncated":true' in actual
    assert reads == [("text-file", 256)] and view.material_refs == [text_ref]
    assert composer._read_archive(context(), ["text-u"])[0].content == message.content

    image_ref = ref("image-file", "image/png")
    image = HumanMessage(id="image-u", content=[{"type": "image", "artifact_ref": image_ref.model_dump()}])
    with pytest.raises(HarnessError, match="Vision"):
        await composer.compose(context(), [image], demo_profile(), [])
    assert len(reads) == 1
    profile = demo_profile().model_copy(
        update={
            "capabilities": {
                **demo_profile().capabilities,
                "vision": Capability(status="verified", evidence_ref="wire-fixture-only"),
            }
        }
    )
    view = await composer.compose(context(), [image], profile, [])
    block = composer.materialize(context(), view)["messages"][-1].content[0]
    assert block == {
        "type": "image",
        "mime_type": "image/png",
        "base64": base64.b64encode(payloads["image-file"]).decode(),
    }
    binary = HumanMessage(
        id="binary-u",
        content=[
            {
                "type": "file_reference",
                "artifact_ref": text_ref.model_copy(update={"mime_type": "application/pdf"}).model_dump(),
            }
        ],
    )
    with pytest.raises(HarnessError, match="Binary"):
        await composer.compose(context(), [binary], profile, [])
