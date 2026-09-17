import pytest

from harness.core import HarnessError
from harness.platform.config import Settings
from harness.server.composition import Services
from harness.scheduler.worker import Worker
from test_integration import setup_services, submit, advance


def manifest(version="1.0.0", **contributes):
    return {"id": "fixture-plugin", "version": version, "harness_api": ">=1,<2", "contributes": contributes}


@pytest.mark.asyncio
async def test_plugin_agent_ports_snapshot_drain_restart_and_terminal_release(tmp_path):
    services, _, session = setup_services(tmp_path)
    record = services.dispatch("load_plugin", "local", body={"manifest": manifest(agent_templates=[
        {"id": "plugin-agent", "tools": ["request_input"]}])})
    session = services.dispatch("create_session", "local", body={"workspace_id": session["workspace_id"],
        "agent_spec_id": "plugin-agent", "title": "Plugin fixture"})
    await services.open_runtime()
    worker = Worker(services)
    try:
        run_id = submit(services, session, '/tool request_input {"prompt":"Continue?"}')
        run = await advance(worker, run_id, {"waiting_user", "failed"})
        assert run["status"] == "waiting_user", run
        snapshot = services.store.get("snapshots", run["snapshot_id"])
        assert snapshot["plugins"] == [{"plugin_id": "fixture-plugin", "version": "1.0.0", "manifest_hash": record["manifest_hash"]}]
        interaction = services.store.interactions(run_id)[0]
        assert services.plugins.drain("fixture-plugin", "1.0.0")["status"] == "draining"
        with pytest.raises(HarnessError, match="停止接受"):
            submit(services, session, "new task")
        # A newer contribution is the default while the old run stays locked.
        services.dispatch("load_plugin", "local", body={"manifest": manifest("2.0.0", agent_templates=[
            {"id": "plugin-agent", "tools": []}])})
        await worker.drain()
        await services.close()
        services = Services(Settings(data_dir=tmp_path / "data", instance_id="plugin-restarted"))
        assert services.store.get("config/agents", "plugin-agent")["tools"] == []
        assert len(services.plugins.references("fixture-plugin", "1.0.0")) == 1
        await services.open_runtime()
        worker = Worker(services)
        services.scheduler.respond("local", interaction["id"], {"expected_revision": interaction["revision"],
            "response": {"text": "yes"}})
        run = await advance(worker, run_id, {"completed", "failed", "needs_review"})
        assert run["status"] == "completed", run
        assert services.store.get("plugins", "fixture-plugin@1.0.0")["status"] == "disposed"
        assert not services.plugins.references("fixture-plugin", "1.0.0")
        assert services.store.get("config/agents", "plugin-agent")["tools"] == []
        assert services.plugins.drain("fixture-plugin", "2.0.0")["status"] == "disposed"
        assert services.store.get("config/agents", "plugin-agent") is None
    finally:
        await worker.drain()
        await services.close()


def test_plugin_static_ports_are_atomic_untrusted_and_no_executable_shell(tmp_path):
    services, _, _ = setup_services(tmp_path)
    root = tmp_path / "skills"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: fixture\ndescription: fixture skill\n---\nBody", encoding="utf-8")
    bad = manifest(skill_sources=[{"id": "fixture-skills", "root": str(root)}], tools=[{"id": "unimplemented"}])
    with pytest.raises(ValueError, match="unavailable"):
        services.plugins.load(bad)
    assert not services.store.list("plugin_contributions")
    assert not services.store.list("plugins")
    services.dispatch("load_plugin", "local", body={"manifest": manifest(skill_sources=[{"id": "fixture-skills", "root": str(root)}],
        mcp_connectors=[{"id": "fixture-mcp", "display_name": "Fixture", "transport": "streamable_http", "endpoint": "http://127.0.0.1:8768/mcp"}])})
    assert services.store.get("config/skill_sources", "fixture-skills")["trusted"] is False
    assert services.resolve_mcp_profile("fixture-mcp").server_id == "fixture-mcp"
    skill = next(s for s in services.store.list("skills") if s["source_id"] == "fixture-skills")
    assert skill["trust_state"] == "unreviewed"
    # Rehydration does not overwrite a user's trust/config changes or advance revisions.
    config = services.store.get("config/skill_sources", "fixture-skills")
    services.store.put("config/skill_sources", "fixture-skills", {**config, "enabled": False})
    services.plugin_manager.recover()
    assert not services.store.get("config/skill_sources", "fixture-skills")["enabled"]
    services.dispatch("drain_plugin", "local", plugin_id="fixture-plugin", version="1.0.0")
    assert services.store.get("config/mcp", "fixture-mcp") is None
    assert services.store.get("config/skill_sources", "fixture-skills") is None
