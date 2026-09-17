"""Vertical tests: production composition, worker, graph, policy, tools and SQLite."""

from __future__ import annotations

import asyncio

import pytest

from harness.platform.config import Settings
from harness.scheduler import SchedulerConfig
from harness.scheduler.worker import Worker
from harness.server.composition import Services


def setup_services(tmp_path):
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    services = Services(
        Settings(
            data_dir=tmp_path / "data",
            instance_id="fixture-instance",
            permissions={"network": True, "process": True},
        )
    )
    workspace = services.dispatch(
        "create_workspace", "local", body={"name": "Fixture", "root_candidate": str(workspace_path)}
    )
    session = services.dispatch(
        "create_session",
        "local",
        body={"workspace_id": workspace["id"], "agent_spec_id": "default", "title": "Fixture"},
    )
    return services, workspace_path, session


def submit(services, session, text, branch_id=None):
    branch = services.store.branch(branch_id or session["default_branch_id"])
    return services.scheduler.submit(
        "local",
        session["id"],
        {
            "branch_id": branch["id"],
            "expected_branch_revision": branch["revision"],
            "content_parts": [{"type": "text", "text": text}],
        },
    )["run_id"]


async def advance(worker, run_id, statuses, limit=300):
    for _ in range(limit):
        await worker.tick()
        run = worker.store.run(run_id)
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run did not reach {statuses}: {worker.store.run(run_id)}")


@pytest.mark.asyncio
async def test_production_worker_text_tool_approval_and_input(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    await services.open_runtime()
    worker = Worker(services)
    try:
        run_id = submit(services, session, "hello")
        run = await advance(worker, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        messages = services.store.messages(session["default_branch_id"], run_id)
        assert len([m for m in messages if m["role"] == "user"]) == 1
        assert len([m for m in messages if m["role"] == "assistant"]) == 1
        assert services.store.read_ephemeral(run_id, 0)
        assert run_id not in services.runtime._graphs
        assert run_id not in services.runtime._contexts
        assert run_id not in services.runtime._events
        run_id = submit(
            services,
            session,
            '/tool write_file {"path":"created.txt","content":"approved write","expected_hash":null}',
        )
        run = await advance(worker, run_id, {"waiting_user", "failed"})
        assert run["status"] == "waiting_user", run["error"]
        assert not (workspace / "created.txt").exists()
        interaction = services.store.interactions(run_id)[0]
        services.scheduler.respond(
            "local",
            interaction["id"],
            {
                "expected_revision": interaction["revision"],
                "binding_ref": interaction["binding_ref"],
                "response": {"decision": "approve_once"},
            },
        )
        run = await advance(worker, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert (workspace / "created.txt").read_text() == "approved write"
        assert len(services.store.operations(run_id)) == 1
        run_id = submit(services, session, '/tool request_input {"prompt":"Which language?"}')
        run = await advance(worker, run_id, {"waiting_user", "failed"})
        assert run["status"] == "waiting_user", run["error"]
        item = services.store.interactions(run_id)[0]
        services.scheduler.respond(
            "local", item["id"], {"expected_revision": item["revision"], "response": {"text": "Python"}}
        )
        run = await advance(worker, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "Python" in services.store.get("runtime_results", run_id)["content"]
        attachment = services.artifacts.put_bytes(
            "local", run["workspace_id"], "这是实际读取的UTF8附件".encode(), "text/plain", "upload"
        )
        branch = services.store.branch(session["default_branch_id"])
        attached_run = services.scheduler.submit(
            "local",
            session["id"],
            {
                "branch_id": branch["id"],
                "expected_branch_revision": branch["revision"],
                "content_parts": [
                    {"type": "file_reference", "artifact_ref": attachment.model_dump(mode="json")}
                ],
            },
        )["run_id"]
        run = await advance(worker, attached_run, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert "这是实际读取的UTF8附件" in services.store.get("runtime_results", attached_run)["content"]
    finally:
        await worker.drain()
        await services.close()


@pytest.mark.asyncio
async def test_one_worker_slot_parent_releases_for_child(tmp_path):
    services, _workspace, session = setup_services(tmp_path)
    services.scheduler.config = SchedulerConfig(worker_slots=1)
    await services.open_runtime()
    worker = Worker(services)
    try:
        run_id = submit(
            services,
            session,
            '/tool delegate {"goal":"say hello","completion_criteria":"return a labelled demo answer"}',
        )
        run = await advance(worker, run_id, {"completed", "failed"}, 600)
        assert run["status"] == "completed", run["error"]
        children = services.store.children(run_id)
        assert len(children) == 1 and children[0]["status"] == "completed"
        assert services.store.account(run_id)["consumed"]["children"] == 1
        assert services.store.interactions(run_id) == []
    finally:
        await worker.drain()
        await services.close()


@pytest.mark.asyncio
async def test_steering_persists_once_and_fork_retains_working_history(tmp_path):
    services, _workspace, session = setup_services(tmp_path)
    await services.open_runtime()
    worker = Worker(services)
    try:
        run_id = submit(services, session, "original")
        original = services.store.run(run_id)
        steering = services.scheduler.steer("local", run_id, "latest correction", original["input_revision"])
        run = await advance(worker, run_id, {"completed", "failed"})
        assert run["status"] == "completed", run["error"]
        assert run["input_revision"] == steering["input_revision"]
        assert services.store.pending_input_commands(run_id) == []
        messages = services.store.messages(session["default_branch_id"], run_id)
        assert len([m for m in messages if m["message_id"] == steering["id"]]) == 1
        assert "latest correction" in services.store.get("runtime_results", run_id)["content"]
        source = services.store.branch(session["default_branch_id"])
        services.store.put(
            "context_pins",
            source["id"],
            {
                "revision": 1,
                "items": [
                    {"id": "fixture-pin", "text": "Preserve this source constraint", "source_ref": "user"}
                ],
            },
        )
        branch = await services.create_branch(
            "local",
            session["id"],
            {
                "source_checkpoint_ref": run["checkpoint_ref"],
                "expected_branch_revision": source["revision"],
                "side_effect_policy": "preserve_external",
            },
        )
        assert services.store.get("context_pins", branch["id"])["items"][0]["id"] == "fixture-pin"
        forked = submit(services, session, "fork followup", branch["id"])
        fork_run = await advance(worker, forked, {"completed", "failed"})
        assert fork_run["status"] == "completed", fork_run["error"]
        record = await services.runtime.checkpointer.aget_tuple(
            {"configurable": {"thread_id": branch["graph_thread_key"]}}
        )
        contents = [m.content for m in record.checkpoint["channel_values"]["messages"]]
        assert "original" in contents and "latest correction" in contents and "fork followup" in contents
    finally:
        await worker.drain()
        await services.close()
