import json
import hashlib
import sqlite3
import pytest
from harness.core import HarnessError, OperationKey, new_id, CheckpointRef, InterruptRef, RuntimeOutcome
from harness.policy.engine import DEFAULT_POLICY
from test_integration import setup_services, submit
from harness.storage import Store


def test_future_schema_rejected_without_changing_database(tmp_path):
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_versions(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_versions VALUES(999,'2099-01-01')")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(HarnessError, match="数据格式"):
        Store(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_selected_skill_hash_drift_is_rejected_before_run_is_created(tmp_path):
    services, _, session = setup_services(tmp_path)
    services.skills.refresh()
    skill = services.store.list("skills")[0]
    with pytest.raises(HarnessError, match="内容版本"):
        services.scheduler.submit(
            "local",
            session["id"],
            {
                "branch_id": session["default_branch_id"],
                "expected_branch_revision": 1,
                "content_parts": [{"type": "text", "text": "fixture"}],
                "selected_skill_refs": [{"skill_id": skill["skill_id"], "content_hash": "changed"}],
            },
        )
    assert not services.store.runs(session_id=session["id"])


def test_frozen_local_only_never_expands_when_live_policy_is_relaxed(tmp_path, monkeypatch):
    services, _, session = setup_services(tmp_path)
    services.store.put("config/policies", "default", {**DEFAULT_POLICY, "egress": "local_only"})
    run_id = submit(services, session, "bounded")
    ctx = services.scheduler.claim("fixture")
    services.store.put(
        "config/policies", "default", {**DEFAULT_POLICY, "revision": 2, "egress": "cloud_allowed"}
    )
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    with pytest.raises(HarnessError, match="本地"):
        services.policy.endpoint("https://example.test", ctx, purpose="model", configured=True)
    assert services.store.run(run_id)["status"] == "running"


@pytest.mark.asyncio
async def test_cancel_closes_pending_form_and_rejects_stale_reply(tmp_path):
    services, _, session = setup_services(tmp_path)
    run_id = submit(services, session, "wait")
    ctx = services.scheduler.claim("fixture")
    waiting = await services.gateway.execute(
        ctx,
        OperationKey(run_id=run_id, message_id=new_id(), tool_call_id=new_id()),
        "request_input",
        {"prompt": "choose"},
    )
    checkpoint = CheckpointRef(graph_thread_key=ctx.graph_thread_key, checkpoint_id="fixture")
    services.scheduler.settle(
        ctx,
        RuntimeOutcome(
            kind="waiting",
            run_id=run_id,
            input_revision=ctx.input_revision,
            checkpoint_ref=checkpoint,
            interrupt_refs=[
                InterruptRef(
                    interrupt_id="fixture",
                    checkpoint_ref=checkpoint,
                    kind="user",
                    interaction_id=waiting.interaction_id,
                )
            ],
        ),
    )
    original = services.store.interaction(waiting.interaction_id)
    services.scheduler.cancel("local", run_id)
    assert services.store.interaction(waiting.interaction_id)["status"] == "cancelled"
    assert all(op["state"] == "cancelled" for op in services.store.operations(run_id))
    assert services.store.account(run_id)["reserved"].get("tool_calls", 0) == 0
    with pytest.raises(HarnessError):
        services.scheduler.respond(
            "local",
            waiting.interaction_id,
            {"expected_revision": original["revision"], "response": {"text": "late"}},
        )
    assert any(
        e["type"] == "interaction.resolved" and e["data"]["resolution"] == "cancelled"
        for e in services.store.events(run_id)
    )


@pytest.mark.asyncio
async def test_idempotent_none_receipt_and_content_free_metrics(tmp_path):
    services, _, _ = setup_services(tmp_path)
    count = []

    def callback():
        count.append(1)

    for _ in range(2):
        assert await services.mutate("local", "DELETE", "/api/memories/fixture", "same", {}, callback) is None
    assert len(count) == 1
    services.metrics.record(
        "model.attempt.started", {"model_attempt_id": "fixture", "retry": 0, "prompt": "CANARY_SECRET"}
    )
    services.metrics.record(
        "model.attempt.completed",
        {
            "model_attempt_id": "fixture",
            "first_token_ms": None,
            "usage": {"cost": None, "source": "unknown"},
            "headers": {"Authorization": "CANARY_SECRET"},
        },
    )
    summary = services.metrics.snapshot()
    assert summary["model_first_token_ms"]["count"] == 0
    assert summary["usage_unknown"] == 1
    assert "CANARY_SECRET" not in json.dumps(services.store.list("metrics"))
