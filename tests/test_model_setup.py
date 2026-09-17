"""Three-field setup runs real provider SDK probes against isolated loopback fixtures."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from langchain_core.messages import HumanMessage

from harness.core import HarnessError
from harness.model_gateway import ModelProfile
from harness.platform.config import Settings
from harness.policy.engine import ModelSetupGrant
from harness.server.composition import Services


class MemoryVault:
    def __init__(self):
        self.values = {}

    def put(self, target, value):
        ref = f"credential:{target}:{len(self.values)}"
        self.values[ref] = value
        return ref

    def get(self, ref, target):
        assert ref.startswith(f"credential:{target}:")
        return self.values[ref]

    def delete(self, ref, target):
        self.get(ref, target)
        del self.values[ref]


@pytest.fixture
def model_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            calls.append((self.path, None))
            if self.path.startswith("/redirect"):
                self.send_response(307)
                self.send_header("Location", "/target/models")
                self.end_headers()
                return
            data = {"data": [{"id": "fixture-model"}]}
            if self.path.startswith("/huge"):
                data["padding"] = "x" * (1024 * 1024)
            self.respond(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, body))
            if self.path.startswith("/auth"):
                self.send_response(401)
                self.end_headers()
                self.wfile.write(self.headers.get("Authorization", "").encode())
                return
            if self.path.startswith("/redirect"):
                self.send_response(307)
                self.send_header("Location", "/target/chat/completions")
                self.end_headers()
                return
            tool_call = body.get("tools") and body["messages"][-1]["role"] != "tool"
            names = [tool["function"]["name"] for tool in body.get("tools", [])]
            tool_name = "fixture_lookup" if "fixture_lookup" in names else "list_files"
            message = {"role": "assistant", "content": None if tool_call else "READY"}
            if tool_call:
                message["tool_calls"] = [
                    {
                        "id": "fixed-call",
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": '{"key":"ready"}'
                            if tool_name == "fixture_lookup"
                            else '{"path":"."}',
                        },
                    }
                ]
            self.respond(
                {
                    "id": "fixture-response",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "fixture-model",
                    "choices": [
                        {
                            "index": 0,
                            "message": message,
                            "finish_reason": "tool_calls" if tool_call else "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
                }
            )

        def respond(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", calls
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


@pytest.fixture
def services(tmp_path):
    result = Services(Settings(data_dir=tmp_path / "data"))
    result.vault = MemoryVault()
    return result


@pytest.mark.asyncio
async def test_setup_probe_save_revision_credentials_and_default_context(services, model_server, tmp_path):
    url, calls = model_server
    body = {
        "provider_id": "custom",
        "base_url": url + "/v1",
        "api_key": "canary-secret-one",
        "model_id": "fixture-model",
    }
    setup = services.model_setup
    assert (await setup.discover("local", body))["items"][0]["id"] == "fixture-model"
    assert (await setup.test("local", body))["tool_calling"]
    assert services.vault.values == {}
    assert len(services.store.list("model_profiles")) == 1
    saved = await setup.save("local", body)
    profile = ModelProfile.model_validate(saved["profile"])
    assert profile.supports("text") and profile.supports("tool_calling") and not profile.supports("vision")
    assert services.store.get("config/agents", "default")["model_policy"]["profile_ref"] == profile.ref
    assert services.vault.get(profile.credential_ref, "model-" + profile.profile_id) == body["api_key"]
    ctx = services.diagnostic("local", profile.ref)
    services.check_model_policy(ctx, profile)  # precise grant permits only this model under network=false
    with pytest.raises(HarnessError, match="网络"):
        services.policy.endpoint(profile.endpoint_ref, ctx, purpose="mcp", configured=True)
    updated = await setup.save(
        "local",
        {**body, "api_key": "canary-secret-two", "profile_id": profile.profile_id, "expected_revision": 1},
    )
    assert updated["revision"] == 2
    assert services.models.get_profile(profile.ref).credential_ref == profile.credential_ref
    assert len(services.vault.values) == 2
    assert "canary-secret" not in json.dumps(
        services.store.list("model_profiles") + services.store.list("model_verifications")
    )
    with pytest.raises(HarnessError, match="更新"):
        await setup.save("local", {**body, "profile_id": profile.profile_id, "expected_revision": 1})

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    workspace = services.dispatch(
        "create_workspace", "local", body={"name": "fixture", "root_candidate": str(workspace_dir)}
    )
    session = services.dispatch(
        "create_session", "local", body={"workspace_id": workspace["id"], "agent_spec_id": "default"}
    )
    branch = services.store.branch(session["default_branch_id"])
    run = services.scheduler.submit(
        "local",
        session["id"],
        {
            "branch_id": branch["id"],
            "expected_branch_revision": branch["revision"],
            "content_parts": [{"type": "text", "text": "hello"}],
        },
    )
    execution = services.scheduler.claim("fixture-worker")
    snapshot = services.store.get("snapshots", execution.config_snapshot_id)
    assert snapshot["model_profile"]["revision"] == 2
    view = await services.context.compose(
        execution,
        [HumanMessage(content="hello", id="u")],
        ModelProfile.model_validate(snapshot["model_profile"]),
        snapshot["tools"],
        system_prompt="fixture",
    )
    assert view.budget_breakdown["total"] <= view.budget_breakdown["input_budget"]
    assert run["run_id"] == execution.run_id and len(calls) == 13
    # The product Worker defaults to live streaming; an unverified JSON-only endpoint must
    # still execute through a safe non-streaming fallback after successful setup.
    from harness.scheduler.worker import Worker

    await services.open_runtime()
    worker = Worker(services)
    try:
        await worker._execute(execution)
        outcome = services.store.run(execution.run_id)
        assert outcome["status"] == "completed", outcome["error"]
        assert len(services.store.operations(execution.run_id)) == 1
        assert not calls[-1][1].get("stream", False)
    finally:
        await services.close()


@pytest.mark.asyncio
async def test_setup_redirect_bound_error_and_anonymous_custom(services, model_server):
    url, calls = model_server
    base = {"provider_id": "custom", "base_url": url + "/v1", "model_id": "fixture-model"}
    assert (await services.model_setup.test("local", base))["connected"]
    for path, code in [("/redirect", "MODEL_REDIRECT_DENIED"), ("/huge", "MODEL_CATALOG_TOO_LARGE")]:
        with pytest.raises(HarnessError) as error:
            await services.model_setup.discover("local", {**base, "base_url": url + path})
        assert error.value.code == code
    with pytest.raises(HarnessError) as error:
        await services.model_setup.test(
            "local", {**base, "base_url": url + "/auth", "api_key": "never-echo-this-key"}
        )
    assert error.value.code == "MODEL_AUTH_FAILED" and "never-echo" not in str(error.value)
    with pytest.raises(HarnessError):
        await services.model_setup.test("local", {**base, "base_url": url + "/redirect"})
    assert not any(path.startswith("/target") for path, _ in calls)
    assert services.vault.values == {} and len(services.store.list("model_profiles")) == 1


@pytest.mark.asyncio
async def test_setup_concurrent_save_cas_rolls_back_new_key(services, model_server, monkeypatch):
    url, _ = model_server
    setup = services.model_setup
    body = {"provider_id": "custom", "base_url": url + "/v1", "api_key": "first", "model_id": "fixture-model"}
    saved = await setup.save("local", body)
    entered, release = asyncio.Event(), asyncio.Event()
    original = setup._probe

    async def paused(*args):
        result = await original(*args)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(setup, "_probe", paused)
    pending = asyncio.create_task(
        setup.save("local", {**body, "api_key": "new", "profile_id": saved["id"], "expected_revision": 1})
    )
    await entered.wait()
    old = services.store.get("config/models", saved["id"])
    services.store.compare_and_set("config/models", saved["id"], 1, {**old, "revision": 2})
    release.set()
    with pytest.raises(HarnessError) as error:
        await pending
    assert error.value.code == "REVISION_CONFLICT"
    assert len(services.vault.values) == 1
    assert not services.store.get("model_profiles", saved["id"] + "@2")


def test_model_grants_do_not_bypass_scope_egress_or_ssrf(services, monkeypatch):
    import socket

    ctx = services.diagnostic("local", "fixture")
    url = "https://provider.example/v1"
    grant = ModelSetupGrant("local", ctx.diagnostic_id, "fixture@1", url)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("8.8.8.8", 443))])
    assert services.policy.endpoint(
        url, ctx, purpose="model", configured=True, model_profile_ref="fixture@1", model_setup_grant=grant
    )
    for target, purpose, ref in [
        (url, "fetch", "fixture@1"),
        (url + "/other", "model", "fixture@1"),
        (url, "model", "other@1"),
    ]:
        with pytest.raises(HarnessError):
            services.policy.endpoint(
                target, ctx, purpose=purpose, configured=True, model_profile_ref=ref, model_setup_grant=grant
            )
    services.store.put("config/policies", "default", {"egress": "local_only"})
    with pytest.raises(HarnessError, match="本地"):
        services.policy.endpoint(
            url, ctx, purpose="model", configured=True, model_profile_ref="fixture@1", model_setup_grant=grant
        )
    services.store.put("config/policies", "default", {"egress": "cloud_allowed"})
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", ("169.254.169.254", 443))]
    )
    with pytest.raises(HarnessError, match="私网"):
        services.policy.endpoint(
            url, ctx, purpose="model", configured=True, model_profile_ref="fixture@1", model_setup_grant=grant
        )


@pytest.mark.asyncio
async def test_setup_maintenance_and_pinned_configuration(services):
    body = {"provider_id": "ollama", "model_id": "fixture"}
    services.store.put("system", "maintenance", {"enabled": True})
    with pytest.raises(HarnessError) as error:
        await services.model_setup.save("local", body)
    assert error.value.code == "MAINTENANCE"
    services.store.put("system", "maintenance", {"enabled": False})
    services.settings = services.settings.model_copy(update={"model_profile_ref": "demo@1"})
    with pytest.raises(HarnessError) as error:
        await services.model_setup.save("local", body)
    assert error.value.code == "MODEL_PROFILE_PINNED"
