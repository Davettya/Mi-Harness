"""Scheduling barriers, fencing, root budgets and permit recovery boundaries."""

from __future__ import annotations

import asyncio

import pytest
from test_integration import setup_services, submit

from harness.core import CheckpointRef, HarnessError, InterruptRef, RuntimeOutcome
from harness.scheduler.budget import ModelBudgetAdapter
from harness.scheduler.worker import Worker


def checkpoint(ctx):
    return CheckpointRef(graph_thread_key=ctx.graph_thread_key, checkpoint_id="fixture-checkpoint")


def test_parallel_interrupt_barrier_expiry_is_not_approval(tmp_path):
    services, _workspace, session = setup_services(tmp_path)
    run_id = submit(services, session, "fixture")
    ctx = services.scheduler.claim("fixture-worker")
    refs = []
    items = []
    for index in range(2):
        item = services.store.prepare_interaction(
            ctx,
            f"fixture:{index}",
            "user_input",
            {
                "prompt": f"Question {index}",
                "response_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            },
            "2099-01-01T00:00:00Z",
        )
        refs.append(
            InterruptRef(
                interrupt_id=f"interrupt-{index}",
                checkpoint_ref=checkpoint(ctx),
                kind="user",
                interaction_id=item["id"],
            )
        )
        items.append(item)
    services.scheduler.settle(
        ctx,
        RuntimeOutcome(
            kind="waiting",
            run_id=run_id,
            input_revision=ctx.input_revision,
            checkpoint_ref=checkpoint(ctx),
            interrupt_refs=refs,
        ),
    )
    first = services.store.interaction(items[0]["id"])
    services.scheduler.respond(
        "local", first["id"], {"expected_revision": first["revision"], "response": {"text": "answered"}}
    )
    assert services.store.run(run_id)["status"] == "waiting_user"
    second = services.store.interaction(items[1]["id"])
    services.store._exec(
        "UPDATE interactions SET expires_at=? WHERE id=?", ("2000-01-01T00:00:00Z", second["id"])
    )
    services.scheduler.expire_interactions()
    assert services.store.run(run_id)["status"] == "queued"
    expired = services.store.interaction(second["id"])
    assert expired["resolution"]["outcome"] == "expired"
    assert "response" not in expired["resolution"]
    assert len([e for e in services.store.events(run_id) if e["type"] == "run.queued"]) == 2
    services.scheduler.expire_interactions()
    assert len([e for e in services.store.events(run_id) if e["type"] == "run.queued"]) == 2
    with pytest.raises(HarnessError):
        services.scheduler.respond(
            "local",
            expired["id"],
            {"expected_revision": expired["revision"], "response": {"text": "too late"}},
        )


def test_expired_dead_worker_recovery_fences_original_attempt(tmp_path):
    services, _workspace, session = setup_services(tmp_path)
    run_id = submit(services, session, "fixture")
    original = services.scheduler.claim("dead", pid=99999999, process_start=1)
    services.store._exec(
        "UPDATE run_attempts SET lease_expiry=? WHERE id=?",
        ("2000-01-01T00:00:00Z", original.worker_attempt_id),
    )
    report = services.scheduler.recover()
    assert report == [{"run_id": run_id, "state": "queued"}]
    current = services.scheduler.claim("replacement")
    assert current.run_id == original.run_id
    assert current.fencing_token == original.fencing_token + 1
    with pytest.raises(HarnessError, match="租约"):
        services.store.assert_fence(original)
    services.store.assert_fence(current)


@pytest.mark.asyncio
async def test_global_permit_and_unknown_budget_remain_distinct(tmp_path):
    services, _workspace, _session = setup_services(tmp_path)
    ctx = services.diagnostic("local", "demo@1")
    one = ModelBudgetAdapter(services.store, services.scheduler, concurrency=1)
    two = ModelBudgetAdapter(services.store, services.scheduler, concurrency=1)
    first = await one.reserve(ctx, "one", 100, 20)
    waiting = asyncio.create_task(two.reserve(ctx, "two", 100, 20))
    await asyncio.sleep(0.1)
    assert not waiting.done()
    await one.settle(ctx, first, {"source": "unknown", "cost": None})
    second = await asyncio.wait_for(waiting, 2)
    assert services.store.account(ctx.budget_account_id)["reserved"]["total_tokens"] == 240
    await two.settle(ctx, second, {"source": "provider", "total_tokens": 20, "cost": None})
    account = services.store.account(ctx.budget_account_id)
    assert account["reserved"]["total_tokens"] == 120
    assert account["consumed"]["total_tokens"] == 20
    assert services.store.list("model_permits") == []
    with pytest.raises(HarnessError):
        await one.reserve(ctx, "over-limit", 30000, 20)


@pytest.mark.asyncio
async def test_maintenance_drains_without_claiming_queued_tasks(tmp_path):
    services, _workspace, session = setup_services(tmp_path)
    run_id = submit(services, session, "remain queued")
    services.store.put("system", "maintenance", {"enabled": True})
    worker = Worker(services)
    await worker.tick()
    assert services.store.run(run_id)["status"] == "queued"
    assert services.store.get("system", "worker_state")["status"] == "drained"
