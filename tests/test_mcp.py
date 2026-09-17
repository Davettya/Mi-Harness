from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from harness.core.contracts import DiagnosticContext, ExecutionContext, OperationKey
from harness.mcp import McpClientFactory, McpGateway, ConnectionProfile, McpInvocation
from test_mcp_fixture_server import create_server


def context(run="run"):
    return ExecutionContext(owner_id="owner", workspace_id="workspace", session_id="session", branch_id="branch",
        run_id=run, root_run_id=run, worker_attempt_id="attempt", fencing_token=1, graph_thread_key="thread",
        input_revision=1, config_snapshot_id="snapshot", trace_id="trace")


def diagnostic():
    return DiagnosticContext(diagnostic_id="diagnostic", owner_id="owner", workspace_id="workspace",
        config_snapshot_id="snapshot", budget_account_id="budget", deadline_at="2099-01-01T00:00:00Z", trace_id="trace")


def gateway(profile, **kwargs):
    factory = McpClientFactory(authorize=lambda *args: None, **kwargs)
    def validated(inv):
        if inv.policy_decision_ref != "fixture-ledger-approved": raise PermissionError("unapproved")
    return McpGateway(lambda _: profile, factory, invocation_validator=validated)


def invocation(tool, args=None, operation="operation", ctx=None):
    ctx = ctx or context()
    return McpInvocation(operation_key=OperationKey(run_id=ctx.run_id, message_id="message", tool_call_id=operation),
        operation_id=operation, execution_context=ctx, server_id="fixture", original_name=tool.original_name,
        schema_hash=tool.schema_hash, arguments=args or {}, policy_decision_ref="fixture-ledger-approved")


class Endpoint(str):
    def __new__(cls, value, json_response):
        obj = str.__new__(cls, value)
        obj.json_response = json_response
        return obj


@pytest.fixture(params=[False, True], ids=["request-sse", "json-response"])
def http_endpoint(request):
    fixture_server = create_server()
    app = fixture_server.streamable_http_app(json_response=request.param)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    limit = time.monotonic() + 10
    while not server.started and time.monotonic() < limit:
        time.sleep(.01)
    if not server.started:
        raise RuntimeError("local fixture server did not start")
    endpoint = Endpoint(f"http://127.0.0.1:{port}/mcp", request.param)
    endpoint.counts = fixture_server.fixture_counts
    yield endpoint
    server.should_exit = True
    thread.join(10)
    listener.close()
    assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["modern_only", "legacy_only"])
async def test_p01_http_interop_resources_prompts_and_reentrant(http_endpoint, protocol):
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="streamable_http",
        endpoint=http_endpoint, protocol_policy=protocol, allowed_capabilities=["tools", "resources", "prompts"])
    gw = gateway(profile)
    catalog = await gw.discover("fixture", context())
    assert catalog.complete
    assert catalog.protocol_version == ("2026-07-28" if protocol == "modern_only" else "2025-11-25")
    add = next(t for t in catalog.tools if t.original_name == "add")
    result = await gw.invoke(invocation(add, {"a": 20, "b": 22}))
    assert result.kind == "success", result
    assert result.structured_content["sum"] == 42
    again = await gw.invoke(invocation(add, {"a": 2, "b": 3}, "operation-2"))
    assert again.structured_content["sum"] == 5
    resource = await gw.read_resource("fixture", "fixture://greeting", context())
    assert resource.server_id == "fixture" and not resource.trusted_instructions
    assert "Hello" in resource.content["contents"][0]["text"]
    prompt = await gw.get_prompt("fixture", "explain", {"topic": "SDD"}, context())
    assert "SDD" in prompt.content["messages"][0]["content"]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["modern_only", "legacy_only"])
async def test_p01_stdio_interop_and_run_scope(protocol):
    profile = ConnectionProfile(server_id="fixture", display_name="Stdio fixture", transport="stdio",
        command=sys.executable, args=[str(Path(__file__).with_name("test_mcp_fixture_server.py"))],
        protocol_policy=protocol, connection_scope="run")
    gw = gateway(profile)
    try:
        catalog = await gw.discover("fixture", context())
        add = next(t for t in catalog.tools if t.original_name == "add")
        result = await gw.invoke(invocation(add, {"a": 4, "b": 5}))
        assert result.kind == "success", result
        assert result.structured_content["sum"] == 9
        assert len(gw._scopes) == 1
    finally:
        assert (await gw.close_scope("run"))["closed"] == 1


@pytest.mark.asyncio
async def test_p01_stdio_vault_reference_target_binding_and_schema(monkeypatch):
    import keyring
    from harness.core import HarnessError
    from harness.platform.credentials import CredentialVault, assert_secret_refs
    backing = {}
    monkeypatch.setattr(keyring,"set_password",lambda service,identity,value:backing.__setitem__((service,identity),value))
    monkeypatch.setattr(keyring,"get_password",lambda service,identity:backing.get((service,identity)))
    monkeypatch.setenv("HARNESS_AMBIENT_CANARY","never-forward-ambient")
    vault = CredentialVault(service="fixture")
    reference = vault.put("mcp-fixture","synthetic-vault-fixture")
    profile = ConnectionProfile(server_id="fixture",display_name="Vault fixture",transport="stdio",
        command=sys.executable,args=[str(Path(__file__).with_name("test_mcp_fixture_server.py"))],
        environment_refs={"API_KEY":reference},connection_scope="run")
    assert_secret_refs(profile.model_dump(mode="json"))
    with pytest.raises(HarnessError): assert_secret_refs({"environment_refs":{"API_KEY":"plaintext"}})
    for invalid in ({"environment_refs":{"API_KEY":"plaintext"}},{"credential_ref":"plaintext"},{"oauth_ref":"oauth:other"}):
        with pytest.raises(ValueError): ConnectionProfile.model_validate({**profile.model_dump(),**invalid})
    gw = gateway(profile,credential_resolver=lambda ref,server,ctx:vault.get(ref,"mcp-"+server))
    try:
        catalog = await gw.discover("fixture",context())
        tool = next(t for t in catalog.tools if t.original_name=="environment_probe")
        outcome = await gw.invoke(invocation(tool))
        assert outcome.kind=="success"
        assert outcome.structured_content=={"bound_credential":True,"ambient_secret_absent":True}
        with pytest.raises(HarnessError) as error: vault.get(reference,"mcp-other")
        assert error.value.code=="CREDENTIAL_SCOPE"
    finally:
        await gw.aclose()


@pytest.mark.asyncio
async def test_p02_errors_cancel_and_gateway_authorization(http_endpoint):
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="streamable_http", endpoint=http_endpoint)
    gw = gateway(profile)
    catalog = await gw.discover("fixture", context())
    error = next(t for t in catalog.tools if t.original_name == "business_error")
    assert (await gw.invoke(invocation(error))).kind == "tool_error"
    rejected = invocation(error).model_copy(update={"policy_decision_ref": "fake"})
    with pytest.raises(PermissionError): await gw.invoke(rejected)
    slow = next(t for t in catalog.tools if t.original_name == "slow")
    task = asyncio.create_task(gw.invoke(invocation(slow, {"seconds": 10}, "cancel-op")))
    limit = time.monotonic() + 5
    while "cancel-op" not in gw._inflight and time.monotonic() < limit:
        await asyncio.sleep(.01)
    await asyncio.sleep(.05)
    cancellation = await gw.cancel("cancel-op")
    assert cancellation.accepted and cancellation.remote_confirmed is None
    result = await task
    assert result.kind == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["modern_only", "legacy_only"])
async def test_p04_elicitation_refusal_and_diagnostic_no_tools(http_endpoint, protocol):
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="streamable_http", endpoint=http_endpoint,
                                protocol_policy=protocol)
    gw = gateway(profile)
    report = await gw.diagnose("fixture", "full", diagnostic())
    assert report.layers["schema"]["status"] == "passed"
    assert report.layers["example_tool"]["status"] == "not_executed"
    with pytest.raises(TypeError): await gw.diagnose("fixture", "full", context())
    catalog = await gw.discover("fixture", context())
    ask = next(t for t in catalog.tools if t.original_name == "ask")
    result = await gw.invoke(invocation(ask))
    if http_endpoint.json_response and protocol == "legacy_only":
        assert result.kind == "protocol_error"  # This legacy transport has no back-channel.
        assert "unsupported_elicitation" not in result.diagnostics  # Host callback was never reached.
        return
    assert result.kind == "success", result
    assert "unsupported_elicitation" in result.diagnostics


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["modern_only", "legacy_only"])
async def test_mp05_elicitation_bridge(http_endpoint, protocol):
    seen = []
    async def bridge(profile, ctx, params, invocation):
        assert invocation.operation_id == "operation"
        seen.append((ctx.run_id, params.message))
        return {"action": "accept", "content": {"confirmed": True}}
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="streamable_http",
        endpoint=http_endpoint, allowed_capabilities=["tools", "elicitation"], protocol_policy=protocol)
    gw = gateway(profile, elicitation_bridge=bridge)
    catalog = await gw.discover("fixture", context())
    result = await gw.invoke(invocation(next(t for t in catalog.tools if t.original_name == "ask")))
    if http_endpoint.json_response and protocol == "legacy_only":
        assert result.kind == "protocol_error" and not seen
        return
    assert result.kind == "success", result
    assert seen == [("run", "Confirm fixture input")]


class PagesFactory:
    def __init__(self, pages): self.pages, self.calls = pages, []
    def authorize(self, *args): pass
    @asynccontextmanager
    async def connect(self, profile, context, purpose, diagnostics):
        async def list_tools(cursor=None):
            self.calls.append(cursor)
            return self.pages[cursor]
        yield SimpleNamespace(protocol_version="2026-07-28", list_tools=list_tools)


@pytest.mark.asyncio
async def test_p03_empty_repeated_cursor_schema_and_identity_partition():
    tool = lambda name: {"name": name, "inputSchema": {"type": "object"}}
    pages = {None: {"tools": [tool("one")], "nextCursor": ""}, "": {"tools": [tool("two")], "nextCursor": "next"},
             "next": {"tools": [tool("three")], "nextCursor": "next"}}
    factory = PagesFactory(pages)
    profile = ConnectionProfile(server_id="fixture", display_name="Fixture", transport="stdio", command="approved")
    gw = McpGateway(lambda _: profile, factory, invocation_validator=lambda _: None)
    catalog = await gw.discover("fixture", context())
    assert factory.calls == [None, "", "next"]
    assert len(catalog.tools) == 3 and not catalog.complete and "repeated_cursor" in catalog.diagnostics
    other = await gw.discover("fixture", context("other"))
    assert catalog.principal_fingerprint != other.principal_fingerprint
    assert len({t.safe_alias for t in catalog.tools}) == 3
    pages[None] = {"tools": [{"name": "bad", "inputSchema": {"type": "nonsense"}}]}
    refreshed = await gw.discover("fixture", context(), "refresh")
    assert not refreshed.tools and "invalid_schema:bad" in refreshed.diagnostics


@pytest.fixture
def legacy_endpoint():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_server().sse_app(), host="127.0.0.1", port=port,
                                           log_level="error", timeout_graceful_shutdown=1))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    yield f"http://127.0.0.1:{port}/sse"
    server.should_exit = True
    thread.join(5)
    listener.close()
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_p01_explicit_legacy_sse(legacy_endpoint):
    with pytest.raises(ValueError):
        ConnectionProfile(server_id="fixture", display_name="legacy", transport="legacy_sse", endpoint=legacy_endpoint)
    profile = ConnectionProfile(server_id="fixture", display_name="legacy", transport="legacy_sse",
        endpoint=legacy_endpoint, protocol_policy="legacy_only")
    gw = gateway(profile)
    catalog = await gw.discover("fixture", context())
    result = await gw.invoke(invocation(next(t for t in catalog.tools if t.original_name == "add"), {"a": 1, "b": 4}))
    assert catalog.protocol_version == "2025-11-25" and result.kind == "success"
    assert result.structured_content == {"sum": 5}
