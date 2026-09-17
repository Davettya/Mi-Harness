import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from harness.core import OperationKey, new_id
from harness.scheduler.worker import Worker, build_runtime
from harness.platform.config import Settings, Permissions
from harness.server.composition import Services
from test_integration import setup_services, submit, advance


@pytest.mark.asyncio
async def test_os_exit_after_command_effect_requires_review_and_never_replays(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    command = {
        "argv": [
            sys.executable,
            "-c",
            "from pathlib import Path; p=Path('external-effect.txt'); p.open('a').write('once\\n')",
        ],
        "timeout": 10,
    }
    run_id = submit(services, session, "/tool exec " + json.dumps(command))
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("recovery_worker_fixture.py")),
            str(services.data_dir),
            "exec",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and services.store.run(run_id)["status"] != "waiting_user":
            assert process.poll() is None
            await asyncio.sleep(0.05)
        item = next(i for i in services.store.interactions(run_id) if i["status"] == "open")
        services.scheduler.respond(
            "local",
            item["id"],
            dict(
                expected_revision=item["revision"],
                response={"decision": "approve_once"},
                binding_ref=item["binding_ref"],
            ),
        )
        assert await asyncio.to_thread(process.wait, 20) == 23
        assert (workspace / "external-effect.txt").read_text() == "once\n"
        crashed = services.store.run(run_id)
        services.store._exec(
            "UPDATE run_attempts SET lease_expiry=? WHERE id=?",
            ("2000-01-01T00:00:00Z", crashed["attempt_id"]),
        )
        replacement = Services(
            Settings(
                data_dir=services.data_dir,
                instance_id="replacement",
                permissions=Permissions(network=True, process=True),
            )
        )
        await replacement.open()
        async with build_runtime(replacement):
            worker = Worker(replacement)
            await advance(worker, run_id, {"needs_review"})
            for _ in range(3):
                await worker.tick()
        operation = replacement.store.operations(run_id)[0]
        assert operation["state"] == "unknown"
        with pytest.raises(Exception):
            replacement.scheduler.reconcile(
                "local",
                operation["id"],
                {
                    "expected_revision": operation["revision"] - 1,
                    "decision": "accept_unresolved",
                    "note": "stale",
                },
            )
        receipt = replacement.scheduler.reconcile(
            "local",
            operation["id"],
            {
                "expected_revision": operation["revision"],
                "decision": "accept_unresolved",
                "note": "隔离测试已确认文件存在，但接受命令整体结果仍未决并停止任务",
            },
        )
        assert receipt["status"] == "cancelled"
        assert replacement.store.operation(operation["id"])["state"] == "unknown"
        assert (workspace / "external-effect.txt").read_text() == "once\n"
        assert len(replacement.store.reconciliations(operation["id"])) == 1
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


@pytest.mark.asyncio
async def test_os_process_exit_after_file_effect_before_ledger_recovers_without_rewrite(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    run_id = submit(
        services, session, '/tool write_file {"path":"once.txt","content":"one effect","expected_hash":null}'
    )
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("recovery_worker_fixture.py")), str(services.data_dir)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and services.store.run(run_id)["status"] != "waiting_user":
            assert process.poll() is None
            await asyncio.sleep(0.05)
        run = services.store.run(run_id)
        assert run["status"] == "waiting_user"
        item = next(i for i in services.store.interactions(run_id) if i["status"] == "open")
        services.scheduler.respond(
            "local",
            item["id"],
            dict(
                expected_revision=item["revision"],
                response={"decision": "approve_once"},
                binding_ref=item["binding_ref"],
            ),
        )
        assert await asyncio.to_thread(process.wait, 20) == 23
        file = workspace / "once.txt"
        assert file.read_text() == "one effect"
        modified = file.stat().st_mtime_ns
        operation = services.store.operations(run_id)[0]
        assert operation["state"] == "running"
        crashed = services.store.run(run_id)
        services.store._exec(
            "UPDATE run_attempts SET lease_expiry=? WHERE id=?",
            ("2000-01-01T00:00:00Z", crashed["attempt_id"]),
        )
        replacement = Services(
            Settings(
                data_dir=services.data_dir,
                instance_id="replacement",
                permissions=Permissions(network=True, process=True),
            )
        )
        await replacement.open()
        async with build_runtime(replacement):
            worker = Worker(replacement)
            await advance(worker, run_id, {"completed"})
        assert file.stat().st_mtime_ns == modified
        operations = replacement.store.operations(run_id)
        assert len(operations) == 1 and operations[0]["state"] == "succeeded"
        assert operations[0]["id"] == operation["id"]
        assert not replacement.artifacts.integrity()
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 5)


def test_cancel_barrier_prevents_child_and_unknown_never_replays(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    run_id = submit(services, session, "fixture")
    ctx = services.scheduler.claim("old", pid=99999999, process_start=1)
    key = OperationKey(run_id=run_id, message_id=new_id(), tool_call_id=new_id())
    first = services.scheduler.delegate(
        ctx, key, {"goal": "bounded", "completion_criteria": "one answer", "capabilities": ["file_read"]}
    )
    assert (
        services.scheduler.delegate(
            ctx, key, {"goal": "bounded", "completion_criteria": "one answer", "capabilities": ["file_read"]}
        )["id"]
        == first["id"]
    )
    services.scheduler.cancel("local", run_id)
    with pytest.raises(Exception):
        services.scheduler.delegate(
            ctx,
            OperationKey(run_id=run_id, message_id=new_id(), tool_call_id=new_id()),
            {"goal": "late", "completion_criteria": "never"},
        )
    assert len(services.store.children(run_id)) == 1
    assert services.store.run(first["id"])["status"] == "cancelled"
