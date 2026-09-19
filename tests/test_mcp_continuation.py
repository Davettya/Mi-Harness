import asyncio
import json
import pytest

from harness.platform.config import Settings
from harness.server.composition import Services
from harness.scheduler.worker import Worker
from harness.mcp import ConnectionProfile, McpContinuationService, register_catalog
from test_mcp import http_endpoint
from test_mcp_authorization import FixtureVault
from test_integration import setup_services, submit, advance
from harness.core import OperationKey


def with_vault(services, vault):
    services.vault = vault
    services.mcp_continuations = McpContinuationService(services.store, vault)


def save_mcp_fixture(services, profile):
    value = {k: v for k, v in profile.model_dump(mode="json").items() if k not in {"server_id", "config_revision"}}
    text = json.dumps({"schema_version": 1, "mcpServers": {profile.server_id: {**value, "enabled": True}}})
    services.mcp_file.save(text, services.mcp_file.view()["hash"])


@pytest.mark.asyncio
async def test_legacy_profile_elicitation_flag_preserves_noninteractive_tools(tmp_path,http_endpoint):
    services, _, session = setup_services(tmp_path)
    profile = ConnectionProfile(server_id="fixture",display_name="Legacy fixture",transport="streamable_http",
        endpoint=str(http_endpoint),protocol_policy="legacy_only",allowed_capabilities=["tools","elicitation"])
    save_mcp_fixture(services, profile)
    catalog = await services.mcp.discover("fixture",services.diagnostic("local","mcp:fixture",session["workspace_id"]))
    services.store.put("mcp_catalogs",f"local:{session['workspace_id']}:fixture",catalog.model_dump(mode="json"))
    register_catalog(services.mcp,services.registry,catalog,trusted_effects={"add":"read"},continuations=services.mcp_continuations)
    tool = next(t for t in catalog.tools if t.original_name=="add")
    agent = services.store.get("config/agents","default")
    services.store.put("config/agents","default",{**agent,"mcp_servers":["fixture"]})
    run_id = submit(services,session,"add fixture values")
    ctx = services.scheduler.claim("legacy-fixture")
    try:
        outcome = await services.gateway.execute(ctx,OperationKey(run_id=run_id,message_id="fixture",tool_call_id="add"),tool.safe_alias,{"a":1,"b":2})
        assert outcome.status=="succeeded",outcome
        assert not services.store.interactions(run_id)
    finally:
        await services.close()


@pytest.mark.asyncio
async def test_mp05_modern_mrtr_production_gateway_checkpoint_restart_and_pinned_profile(tmp_path, http_endpoint):
    services, workspace, session = setup_services(tmp_path)
    vault = FixtureVault()
    with_vault(services, vault)
    profile = ConnectionProfile(server_id="fixture", display_name="Durable fixture", transport="streamable_http",
        endpoint=str(http_endpoint), protocol_policy="modern_only", allowed_capabilities=["tools", "elicitation"])
    save_mcp_fixture(services, profile)
    diagnostic = services.diagnostic("local", "mcp:fixture", session["workspace_id"])
    catalog = await services.mcp.discover("fixture", diagnostic)
    services.store.put("mcp_catalogs", f"local:{session['workspace_id']}:fixture", catalog.model_dump(mode="json"))
    register_catalog(services.mcp, services.registry, catalog, continuations=services.mcp_continuations)
    tool = next(t for t in catalog.tools if t.original_name == "ask")
    agent = services.store.get("config/agents", "default")
    services.store.put("config/agents", "default", {**agent, "revision": agent["revision"] + 1,
        "tools": [*agent["tools"], tool.safe_alias], "mcp_servers": ["fixture"]})
    await services.open_runtime()
    worker = Worker(services)
    restarted = None
    try:
        run_id = submit(services, session, "/tool " + tool.safe_alias + " {}")
        run = await advance(worker, run_id, {"waiting_user", "failed", "needs_review"})
        assert run["status"] == "waiting_user", run
        approval = next(i for i in services.store.interactions(run_id) if i["kind"] == "approval")
        services.scheduler.respond("local", approval["id"], {"expected_revision": approval["revision"],
            "binding_ref": approval["binding_ref"], "response": {"decision": "approve_once"}})
        run = await advance(worker, run_id, {"waiting_user", "failed", "needs_review"})
        assert run["status"] == "waiting_user", run
        forms = [i for i in services.store.interactions(run_id) if i["kind"] == "mcp_elicitation"]
        assert len(forms) == 1, services.store.interactions(run_id)
        form = forms[0]
        operation = services.store.operations(run_id)[0]
        assert operation["state"] == "running"
        assert operation["metadata"]["protocol_continuation"]["status"] == "awaiting_input"
        assert "private-continuation-state" not in json.dumps(operation)
        assert http_endpoint.counts == {"ask_initial": 1, "ask_resumed": 0}
        await worker.drain()
        await services.close()

        # Recreate every local service and SQLite saver. Only vault + remote server survive.
        restarted = Services(services.settings.model_copy(update={"instance_id":"fixture-restarted"}))
        with_vault(restarted, vault)
        # Configuration changes affect new runs; continuation retains the old endpoint/revision.
        restarted.store.put("config/mcp", "fixture", profile.model_copy(update={"config_revision": 2,
            "endpoint": "http://127.0.0.1:1/unreachable"}).model_dump(mode="json"))
        await restarted.open_runtime()
        worker = Worker(restarted)
        restarted.scheduler.respond("local", form["id"], {"expected_revision": form["revision"],
            "response": {"responses": {"confirmation": {"action": "accept", "content": {"confirmed": True}}}}})
        run = await advance(worker, run_id, {"completed", "failed", "needs_review"})
        assert run["status"] == "completed", run
        operations = restarted.store.operations(run_id)
        assert len(operations) == 1 and operations[0]["id"] == operation["id"] and operations[0]["state"] == "succeeded"
        assert http_endpoint.counts == {"ask_initial": 1, "ask_resumed": 1}
        assert restarted.store.account(run_id)["consumed"]["tool_calls"] == 1
    finally:
        await worker.drain()
        await (restarted or services).close()
