import asyncio
from io import BytesIO

import pytest
from PIL import Image

from harness.core import HarnessError, OperationKey, content_hash
from harness.model_gateway import Capability, estimate_tokens
from harness.model_gateway.profiles import demo_profile
from harness.scheduler.worker import Worker
from harness.server.composition import Services
from test_integration import setup_services, submit, advance


def second_model(services, identity="second", vision=False):
    profile = demo_profile().model_copy(update={"profile_id": identity})
    if vision:
        profile = profile.model_copy(
            update={
                "capabilities": {
                    **profile.capabilities,
                    "vision": Capability(status="verified", evidence_ref="test:static-raster"),
                }
            }
        )
    services.models.register(profile)
    services.store.put("config/models", identity, {"id": identity, **profile.model_dump(mode="json")})
    return profile


def select(services, run_id, ref, revision=0, **kwargs):
    return services.selection.select(
        "local", run_id, dict(model_profile_ref=ref, expected_control_revision=revision, **kwargs)
    )


async def test_switch_waiting_tool_survives_service_restart_and_preserves_snapshots(tmp_path):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    await services.open_runtime()
    worker = Worker(services)
    rid = submit(services, session, '/tool request_input {"prompt":"Which language?"}')
    waiting = await advance(worker, rid, {"waiting_user", "failed"})
    assert waiting["status"] == "waiting_user", waiting["error"]
    frozen = services.store.get("snapshots", waiting["snapshot_id"])
    initial_revision = waiting["input_revision"]
    select(services, rid, "second@1", persist_for_session=True, expected_preferences_revision=1)
    assert services.store.run(rid)["input_revision"] == initial_revision
    await services.close()
    services = Services(services.settings)
    item = services.store.interactions(rid)[0]
    services.scheduler.respond(
        "local", item["id"], dict(expected_revision=item["revision"], response={"text": "Chinese"})
    )
    await services.open_runtime()
    worker = Worker(services)
    try:
        done = await advance(worker, rid, {"completed", "failed", "needs_review"})
        assert done["status"] == "completed", done["error"]
        messages = [m for m in services.store.messages(done["branch_id"], rid) if m["role"] == "assistant"]
        assert [m["profile_ref"] for m in messages] == ["demo@1", "second@1"]
        assert all(m["logical_call_id"] and m["model_attempt_id"] for m in messages)
        assert len(services.store.operations(rid)) == 1
        assert frozen == services.store.get("snapshots", done["snapshot_id"])
        assert services.selection.preferences("local", session["id"])["model_profile_ref"] == "second@1"
    finally:
        await worker.drain()
        await services.close()


async def test_context_preparation_race_last_selection_wins(tmp_path):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    second_model(services, "third")
    await services.open_runtime()
    rid = submit(services, session, "hello")
    select(services, rid, "second@1")
    fired = False

    async def hook(point, ctx):
        nonlocal fired
        if point == "model_context_prepared" and not fired:
            fired = True
            select(services, rid, "third@1", 1)

    services.runtime.fault_hook = hook
    worker = Worker(services)
    try:
        done = await advance(worker, rid, {"completed", "failed"})
        assert done["status"] == "completed", done["error"]
        bindings = [b for b in services.store.list("model_call_bindings") if b["run_id"] == rid]
        assert len(bindings) == 1 and bindings[0]["profile_ref"] == "third@1"
        assert sorted(c["status"] for c in services.store.list("run_model_commands")) == [
            "applied",
            "superseded",
        ]
    finally:
        await worker.drain()
        await services.close()


async def test_rejected_switch_waits_and_explicit_reselection_recovers(tmp_path, monkeypatch):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    await services.open_runtime()
    rid = submit(services, session, "hello")
    select(services, rid, "second@1")
    original = services.models.switch

    async def reject(ref, *args, **kwargs):
        if ref == "second@1":
            raise HarnessError("context_overflow", "Target cannot fit complete history", 422)
        return await original(ref, *args, **kwargs)

    monkeypatch.setattr(services.models, "switch", reject)
    worker = Worker(services)
    try:
        waiting = await advance(worker, rid, {"waiting_user", "failed"})
        assert waiting["status"] == "waiting_user", waiting["error"]
        assert services.selection.control(rid)["status"] == "rejected"
        assert not services.store.list("model_call_bindings")
        select(services, rid, "demo@1", 1)
        interaction = services.store.interactions(rid)[0]
        services.scheduler.respond(
            "local",
            interaction["id"],
            {"expected_revision": interaction["revision"], "response": {"continue": True}},
        )
        done = await advance(worker, rid, {"completed", "failed"})
        assert done["status"] == "completed", done["error"]
        assert services.store.list("model_call_bindings")[0]["profile_ref"] == "demo@1"
    finally:
        await worker.drain()
        await services.close()


async def test_bound_call_keeps_profile_and_terminal_does_not_add_call(tmp_path):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    await services.open_runtime()
    rid = submit(services, session, "hello")

    async def hook(point, ctx):
        if point == "model_bound_before_dispatch":
            select(services, rid, "second@1", persist_for_session=True, expected_preferences_revision=1)

    services.runtime.fault_hook = hook
    worker = Worker(services)
    try:
        done = await advance(worker, rid, {"completed", "failed"})
        assert done["status"] == "completed", done["error"]
        assert len(services.store.list("model_call_bindings")) == 1
        control = services.selection.control(rid)
        assert control["status"] == "not_applied" and control["effective_profile_ref"] == "demo@1"
        with pytest.raises(HarnessError, match="结束"):
            select(services, rid, "demo@1", 1)
    finally:
        await worker.drain()
        await services.close()


async def test_preferences_atomic_conflict_plan_double_guard_and_child_inheritance(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    second_model(services)
    prefs = services.selection.preferences("local", session["id"])
    services.selection.update_preferences("local", session["id"], dict(expected_revision=1, mode="plan"))
    rid = submit(services, session, "plan")
    with pytest.raises(HarnessError):
        select(services, rid, "second@1", persist_for_session=True, expected_preferences_revision=1)
    assert services.selection.control(rid)["control_revision"] == 0
    assert (
        services.selection.preferences("local", session["id"])["model_profile_ref"]
        == prefs["model_profile_ref"]
    )
    ctx = services.scheduler.claim("test")
    snapshot = services.store.get("snapshots", ctx.config_snapshot_id)
    assert snapshot["mode"] == "plan" and "write_file" not in {t["id"] for t in snapshot["tools"]}
    # Even a forged catalog entry or a dangerous MCP readOnlyHint cannot bypass the execution guard.
    write, _ = services.registry.get("write_file")
    services.store.put(
        "snapshots", ctx.config_snapshot_id, {**snapshot, "tools": [*snapshot["tools"], write.model_dump()]}
    )
    with pytest.raises(HarnessError) as exc:
        await services.gateway.execute(
            ctx,
            OperationKey(run_id=rid, message_id="m", tool_call_id="t"),
            "write_file",
            dict(path="bad.txt", content="bad", expected_hash=None),
        )
    assert exc.value.code == "PLAN_TOOL_DENIED" and not (workspace / "bad.txt").exists()
    child = services.scheduler.delegate(
        ctx,
        OperationKey(run_id=rid, message_id="m", tool_call_id="child"),
        dict(goal="read", completion_criteria="report", capabilities=["file_read"]),
    )
    assert services.store.get("snapshots", child["snapshot_id"])["mode"] == "plan"
    with pytest.raises(HarnessError):
        services.scheduler.delegate(
            ctx,
            OperationKey(run_id=rid, message_id="m", tool_call_id="unsafe"),
            dict(goal="write", completion_criteria="done", capabilities=["file_write"]),
        )
    await services.close()


def png():
    out = BytesIO()
    Image.new("RGB", (640, 480), "blue").save(out, format="PNG")
    return out.getvalue()


def test_summary_policy_follows_explicit_selection_unless_independent(tmp_path):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    agent = services.store.get("config/agents", "default")
    services.store.put(
        "config/agents",
        "default",
        {**agent, "model_policy": {**agent["model_policy"], "summary_profile_ref": "demo@1"}},
    )
    snapshot = services.make_snapshot("local", session, {"model_profile_ref": "second@1"})
    assert snapshot["agent_spec"]["model_policy"]["summary_profile_ref"] == "second@1"
    agent = services.store.get("config/agents", "default")
    second_model(services, "summary")
    services.store.put(
        "config/agents",
        "default",
        {**agent, "model_policy": {**agent["model_policy"], "summary_profile_ref": "summary@1"}},
    )
    snapshot = services.make_snapshot("local", session, {"model_profile_ref": "second@1"})
    assert snapshot["agent_spec"]["model_policy"]["summary_profile_ref"] == "summary@1"


async def test_inline_images_order_ownership_budget_and_no_base64_archive(tmp_path):
    from langchain_core.messages import HumanMessage

    services, _, session = setup_services(tmp_path)
    profile = second_model(services, vision=True)
    ref = services.artifacts.put_bytes("local", session["workspace_id"], png(), "image/png").model_dump(
        mode="json"
    )
    parts = [
        dict(type="text", text="A"),
        dict(type="image", artifact_ref=ref),
        dict(type="text", text="B"),
        dict(type="image", artifact_ref=ref),
    ]
    branch = services.store.branch(session["default_branch_id"])
    request = dict(branch_id=branch["id"], expected_branch_revision=branch["revision"], content_parts=parts)
    with pytest.raises(HarnessError) as exc:
        services.scheduler.submit("local", session["id"], request)
    assert exc.value.code == "VISION_UNVERIFIED"
    request["model_profile_ref"] = profile.ref
    rid = services.scheduler.submit("local", session["id"], request)["run_id"]
    assert services.store.run(rid)["input"]["content_parts"] == parts
    assert len(services.store.run_artifacts(rid)) == 1
    ctx = services.scheduler.claim("test")
    view = await services.context.compose(ctx, [HumanMessage(id="u", content=parts)], profile, [])
    materialized = services.context.materialize(ctx, view)["messages"][-1].content
    assert [p["type"] for p in materialized] == ["text", "image", "text", "image"]
    assert "base64" not in str(services.store.list("context_views"))
    assert estimate_tokens({"messages": materialized}, profile).categories["images"] == 8192
    with pytest.raises(HarnessError):
        select(services, rid, "demo@1")
    await services.close()


def test_invalid_image_and_reference_rejected_before_enqueue(tmp_path):
    services, _, session = setup_services(tmp_path)
    for mime, data in [("image/png", b"<svg/>"), ("image/svg+xml", b"<svg/>"), ("image/jpeg", png())]:
        with pytest.raises(HarnessError):
            services.artifacts.put_bytes("local", session["workspace_id"], data, mime)
    ref = services.artifacts.put_bytes("local", session["workspace_id"], b"text", "text/plain").model_dump(
        mode="json"
    )
    ref["content_hash"] = "0" * 64
    branch = services.store.branch(session["default_branch_id"])
    with pytest.raises(HarnessError):
        services.scheduler.submit(
            "local",
            session["id"],
            dict(
                branch_id=branch["id"],
                expected_branch_revision=branch["revision"],
                content_parts=[dict(type="file_reference", artifact_ref=ref)],
            ),
        )
    assert not services.store.runs(session_id=session["id"])


def test_public_agent_readonly_and_local_model_listing(tmp_path):
    services, _, session = setup_services(tmp_path)
    with pytest.raises(HarnessError) as exc:
        services.save_config("agents", "default", {"expected_revision": 1})
    assert exc.value.code == "AGENT_READONLY"
    with pytest.raises(HarnessError):
        services.dispatch(
            "create_session",
            "local",
            body=dict(workspace_id=session["workspace_id"], agent_spec_id="internal"),
        )
    assert services.dispatch("list_agents", "local")["items"][0]["name"] == "Mi Harness"
    before = services.store.list("model_verifications")
    services.selection.available("local", session["id"])
    assert services.store.list("model_verifications") == before


def test_plan_confirmation_is_version_and_branch_bound(tmp_path):
    services, _, session = setup_services(tmp_path)
    rid = submit(services, session, "plan")
    services.store.put("plans", rid, dict(plan_id=rid, revision=2, content_hash=content_hash(["new"])))
    branch = services.store.branch(session["default_branch_id"])
    with pytest.raises(HarnessError) as exc:
        services.scheduler.submit(
            "local",
            session["id"],
            dict(
                branch_id=branch["id"],
                expected_branch_revision=branch["revision"],
                mode="react",
                plan_confirmation=dict(
                    plan_id=rid, revision=1, content_hash=content_hash(["old"]), confirmed=True
                ),
            ),
        )
    assert exc.value.code == "PLAN_CONFIRMATION_STALE"


@pytest.mark.parametrize(
    "stage", ["model_context_prepared", "model_bound_before_dispatch", "model_validated_before_checkpoint"]
)
async def test_os_exit_model_boundaries_recover_binding_and_fence(tmp_path, stage):
    import os
    from pathlib import Path
    import subprocess
    import sys

    services, _, session = setup_services(tmp_path)
    second_model(services)
    rid = submit(services, session, "hello")
    select(services, rid, "second@1")
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("recovery_worker_fixture.py")),
            str(services.data_dir),
            stage,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        assert await asyncio.to_thread(process.wait, 35) == 24
        old_run = services.store.run(rid)
        old_ctx = services.scheduler.context(old_run)
        bindings = services.store.list("model_call_bindings")
        assert len(bindings) == (0 if stage == "model_context_prepared" else 1)
        bound_view_id = bindings[0]["input_view_id"] if bindings else None
        if bound_view_id:
            # A dispatched logical call keeps its input even if pins change before recovery.
            services.store.put("context_pins", old_run["branch_id"], {"revision": 9, "items": []})
        # A later choice cannot rebind a previously dispatched logical step on replay.
        select(services, rid, "demo@1", 1)
        services.store._exec(
            "UPDATE run_attempts SET lease_expiry=? WHERE id=?",
            ("2000-01-01T00:00:00Z", old_run["attempt_id"]),
        )
        replacement = Services(services.settings)
        await replacement.open_runtime()
        worker = Worker(replacement)
        try:
            done = await advance(worker, rid, {"completed", "failed", "needs_review"})
            assert done["status"] == "completed", done["error"]
            bound = replacement.store.list("model_call_bindings")
            assert len(bound) == 1
            assert bound[0]["profile_ref"] == ("demo@1" if stage == "model_context_prepared" else "second@1")
            if bound_view_id:
                assert bound[0]["input_view_id"] == bound_view_id
                assert len(replacement.store.list("context_views")) == 1
            with pytest.raises(HarnessError):
                replacement.selection.candidate(old_ctx, bound[0]["logical_call_id"])
        finally:
            await worker.drain()
            await replacement.close()
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)
        await services.close()


@pytest.mark.parametrize("during", ["stream", "retry"])
async def test_inflight_generation_and_retry_do_not_change_binding(tmp_path, during):
    from langchain_core.messages import AIMessageChunk, AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
    from harness.model_gateway.providers import DemoChatModel

    services, _, session = setup_services(tmp_path)
    second_model(services)
    calls = []
    changed = False
    rid = submit(services, session, '/tool list_files {"path":"."}')

    class ControlledModel(DemoChatModel):
        ref: str

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            nonlocal changed
            calls.append(self.ref)
            if during == "retry" and not changed:
                changed = True
                select(services, rid, "second@1")
                raise TimeoutError("fixture timeout before response")
            if isinstance(messages[-1], ToolMessage):
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="final"))])
            return self._generate(messages, stop, run_manager, **kwargs)

        async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
            nonlocal changed
            calls.append(self.ref)
            if isinstance(messages[-1], ToolMessage):
                yield ChatGenerationChunk(
                    message=AIMessageChunk(content="final", response_metadata={"finish_reason": "stop"})
                )
                return
            yield ChatGenerationChunk(message=AIMessageChunk(content="A:"))
            if not changed:
                changed = True
                select(services, rid, "second@1")
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="complete",
                    tool_call_chunks=[dict(id="call", name="list_files", args='{"path":"."}', index=0)],
                    response_metadata={"finish_reason": "tool_calls"},
                )
            )

    services.models.provider_factory = lambda p, endpoint, secret: ControlledModel(ref=p.ref)
    await services.open_runtime()
    services.models.stream_output = during == "stream"
    worker = Worker(services)
    try:
        done = await advance(worker, rid, {"completed", "failed", "waiting_user"})
        assert done["status"] == "completed", done["error"]
        assert calls == (["demo@1", "second@1"] if during == "stream" else ["demo@1", "demo@1", "second@1"])
        bindings = services.store.list("model_call_bindings")
        assert sorted(b["profile_ref"] for b in bindings) == ["demo@1", "second@1"]
        assert len(services.store.operations(rid)) == 1
    finally:
        await worker.drain()
        await services.close()
