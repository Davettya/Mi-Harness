import asyncio
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from harness.core import (
    HarnessError,
    OperationKey,
    RuntimeOutcome,
    CheckpointRef,
    InterruptRef,
    content_hash,
    new_id,
)
from harness.platform.config import Settings
from harness.server.composition import Services
from harness.storage import Store


@pytest.fixture
def services(tmp_path):
    data = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    services = Services(Settings(data_dir=data, instance_id="test"))
    w = services.dispatch(
        "create_workspace", "local", body=dict(name="temporary", root_candidate=str(workspace))
    )
    session = services.dispatch(
        "create_session", "local", body=dict(workspace_id=w["id"], agent_spec_id="default")
    )
    request = dict(
        branch_id=session["default_branch_id"],
        expected_branch_revision=1,
        agent_spec_revision=1,
        content_parts=[dict(type="text", text="test")],
    )
    receipt = services.scheduler.submit("local", session["id"], request)
    ctx = services.scheduler.claim("test-worker")
    assert ctx.run_id == receipt["run_id"]
    return services, ctx, workspace


def test_canonical_json_and_concurrent_budget(services):
    s, ctx, _ = services
    assert content_hash(dict(a=1, b=2)) == content_hash(dict(b=2, a=1))
    with pytest.raises(ValueError):
        content_hash({"nan": float("nan")})
    s.store.add_account("concurrent", "diagnostic", "account", {"tokens": 10})

    def reserve(i):
        try:
            s.store.reserve("concurrent", str(i), {"tokens": 3})
            return True
        except HarnessError:
            return False

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(reserve, range(10)))
    assert sum(outcomes) == 3
    assert s.store.account("concurrent")["reserved"]["tokens"] == 9


def test_atomic_artifacts_corruption_and_scope(services):
    s, ctx, _ = services
    ref = s.artifacts.writer(ctx, b"evidence", "text/plain", "test")
    assert s.artifacts.read_range(ref.artifact_id, "local") == b"evidence"
    with pytest.raises(HarnessError):
        s.artifacts.path(ref.artifact_id, "someone-else")
    target = s.artifacts.path(ref.artifact_id, "local")
    target.write_bytes(b"broken")
    with pytest.raises(HarnessError, match="产物"):
        s.artifacts.read_range(ref.artifact_id, "local")
    assert len(s.artifacts.integrity()) == 1


async def test_file_approval_binding_replay_and_version_conflict(services):
    s, ctx, workspace = services
    key = OperationKey(run_id=ctx.run_id, message_id=new_id(), tool_call_id=new_id())
    args = dict(path="result.txt", content="safe", expected_hash=None)
    waiting = await s.gateway.execute(ctx, key, "write_file", args)
    assert not (workspace / "result.txt").exists()
    checkpoint = CheckpointRef(graph_thread_key=ctx.graph_thread_key, checkpoint_id="fixture")
    s.scheduler.settle(
        ctx,
        RuntimeOutcome(
            kind="waiting",
            run_id=ctx.run_id,
            input_revision=ctx.input_revision,
            checkpoint_ref=checkpoint,
            interrupt_refs=[
                InterruptRef(
                    interrupt_id="approve",
                    checkpoint_ref=checkpoint,
                    kind="user",
                    interaction_id=waiting.interaction_id,
                )
            ],
        ),
    )
    interaction = s.store.interaction(waiting.interaction_id)
    s.scheduler.respond(
        "local",
        interaction["id"],
        dict(
            expected_revision=interaction["revision"],
            response={"decision": "approve_once"},
            binding_ref=interaction["binding_ref"],
        ),
    )
    resumed = s.scheduler.claim("test-worker")
    result = await s.gateway.execute(resumed, key, "write_file", args)
    assert result.status == "succeeded", result
    assert (workspace / "result.txt").read_text() == "safe"
    repeated = await s.gateway.execute(resumed, key, "write_file", args)
    assert repeated == result
    assert s.store.account(ctx.root_run_id)["consumed"]["tool_calls"] == 1
    with pytest.raises(HarnessError, match="参数"):
        await s.gateway.execute(resumed, key, "write_file", {**args, "content": "different"})


def test_path_escape_and_policy_revocation(services, tmp_path):
    s, ctx, workspace = services
    for path in ("../outside.txt", "\\\\server\\share\\x", str(tmp_path / "outside.txt")):
        with pytest.raises(HarnessError):
            s.policy.path(ctx, path)
    try:
        (workspace / "escape").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pass
    else:
        with pytest.raises(HarnessError):
            s.policy.path(ctx, "escape/outside.txt")
    p = s.store.get("config/policies", "default")
    p.update(capabilities=["file_read"], revision=2)
    s.store.compare_and_set("config/policies", "default", 1, p)
    with pytest.raises(HarnessError):
        s.policy.path(ctx, "x.txt", write=True)


async def test_process_bounded_output_and_tree_timeout(services):
    s, ctx, workspace = services
    operation = dict(id=new_id())
    result = await s.processes.execute(
        ctx,
        operation,
        dict(
            argv=[sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*100*1024*1024)"], timeout=10
        ),
        workspace,
        65536,
    )
    assert result["status"] == "succeeded", result
    assert result["truncated"]
    assert result["structured_data"]["output_bytes"] == 100 * 1024 * 1024
    assert result["structured_data"]["retained_bytes"] == 65536
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid,flush=True); time.sleep(30)"
    result = await s.processes.execute(
        ctx, dict(id=new_id()), dict(argv=[sys.executable, "-c", code], timeout=0.5), workspace, 65536
    )
    assert result["error_code"] == "PROCESS_TIMEOUT"
    import psutil

    pid = int(result["summary"].strip())
    assert not psutil.pid_exists(pid)
    assert not s.processes.active


def test_transaction_rolls_back_and_stale_fence(services):
    s, ctx, _ = services
    with pytest.raises(RuntimeError):
        with s.store.transaction():
            s.store.put("scratch", "x", {"data": 1})
            raise RuntimeError("abort")
    assert s.store.get("scratch", "x") is None
    stale = ctx.model_copy(update={"fencing_token": ctx.fencing_token + 1})
    with pytest.raises(HarnessError):
        s.store.assert_fence(stale)
