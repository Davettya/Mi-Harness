import json
import time
import uuid

import httpx
import pytest

from harness.core import HarnessError
from harness.mcp import ConnectionProfile
from harness.model_gateway.profiles import demo_profile
from harness.platform.config import PlatformConfig, instance_config, load_config
from harness.platform.supervisor import start, stop, status, read_instance, terminate_owned
from harness.platform.cli import doctor
from harness.server.auth import AuthService
from harness.server.composition import Services
from harness.storage import Store
from test_platform import free_port


def test_platform_host_allowlist_can_only_narrow(tmp_path):
    user, workspace = tmp_path / "user.json", tmp_path / "workspace.json"
    user.write_text(json.dumps({"permissions": {"network": True}}))
    workspace.write_text(json.dumps({"permissions": {"allowed_hosts": ["127.0.0.1"]}}))
    settings, _ = load_config(user_file=user, workspace_file=workspace, environ={})
    assert settings.permissions.allowed_hosts == ["127.0.0.1"]
    user.write_text(json.dumps({"permissions": {"network": True, "allowed_hosts": ["example.com"]}}))
    workspace.write_text(json.dumps({"permissions": {"allowed_hosts": []}}))
    settings, explanation = load_config(user_file=user, workspace_file=workspace, environ={})
    assert settings.permissions.allowed_hosts == ["example.com"] and explanation["rejected_overrides"]


def test_default_platform_permission_cap_applies_to_diagnostics(tmp_path):
    services = Services(PlatformConfig(data_dir=tmp_path / "data"))
    context = services.diagnostic("local", "fixture")
    for profile in [ConnectionProfile(server_id="http", display_name="fixture", transport="streamable_http", endpoint="http://127.0.0.1:1/mcp"),
                    ConnectionProfile(server_id="stdio", display_name="fixture", transport="stdio", command="fixture")]:
        with pytest.raises(HarnessError) as error:
            services.policy.authorize_mcp(profile, context, "diagnose")
        assert error.value.code in {"NETWORK_DISABLED", "PROCESS_DISABLED"}
    revision = services.policy.current(context)["revision"]
    services.policy.set_environment_cap({"network": True, "process": False, "workspace_write": True, "allowed_hosts": ["127.0.0.1"]})
    assert services.policy.endpoint("http://127.0.0.1:1/mcp", context, purpose="mcp", configured=True)
    assert services.policy.current(context)["revision"] != revision
    with pytest.raises(HarnessError):
        services.policy.endpoint("https://example.com/", context, purpose="mcp", configured=True)


def test_pf01_real_api_worker_share_effective_config_and_run_cap(tmp_path):
    port, data_dir = free_port(), tmp_path / "data"
    settings = PlatformConfig(data_dir=data_dir, port=port, startup_timeout=45,
        log_level="ERROR", model_profile_ref="alternate@1", permissions={"workspace_write":False})
    bootstrap = Services(settings)
    profile = demo_profile().model_copy(update={"profile_id":"alternate"})
    bootstrap.models.register(profile)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    url = f"http://127.0.0.1:{port}"
    try:
        launched = start(settings)
        assert launched["status"] == "ready", launched
        config = instance_config(data_dir, launched["instance_id"])
        assert config.log_level == "ERROR" and config.model_profile_ref == "alternate@1"
        assert not config.permissions.workspace_write
        with pytest.raises(HarnessError):
            instance_config(data_dir, "other-instance")
        store = Store(data_dir / "app.db")
        with httpx.Client(base_url=url, trust_env=False, timeout=10) as client:
            response = client.post("/api/auth/exchange", json={"ticket":AuthService(store).issue_ticket()}, headers={"Origin":url})
            assert response.status_code == 204, response.text
            client.headers.update({"Origin":url, "X-CSRF-Token":client.cookies["harness_csrf"]})
            def post(path, body):
                response = client.post(path, json=body, headers={"Idempotency-Key":str(uuid.uuid4())})
                assert response.is_success, response.text
                return response.json()
            created = post("/api/workspaces", {"name":"Config fixture", "root_candidate":str(workspace)})
            session = post("/api/sessions", {"workspace_id":created["id"], "agent_spec_id":"default", "title":"Config fixture"})
            branch = store.branch(session["default_branch_id"])
            receipt = post(f"/api/sessions/{session['id']}/runs", {"branch_id":branch["id"], "expected_branch_revision":branch["revision"], "agent_spec_revision":1,
                "content_parts":[{"type":"text","text":"/tool write_file {\"path\":\"forbidden.txt\",\"content\":\"no\",\"expected_hash\":null}"}]})
            run_id = receipt["run_id"]
            deadline = time.monotonic()+35
            while time.monotonic()<deadline:
                run = store.run(run_id)
                if run["status"] in {"completed","failed","waiting_user","needs_review"}: break
                time.sleep(.1)
            assert run["status"] == "failed" and run["error"]["code"] == "CAPABILITY_DENIED", run
            frozen = store.get("snapshots",run["snapshot_id"])
            assert frozen["model_profile"]["profile_id"] == "alternate"
            assert "file_write" not in frozen["policy"]["capabilities"]
            assert not store.interactions(run_id) and not (workspace / "forbidden.txt").exists()
            assert not store.operations(run_id)  # Resource authorization rejects before operation preparation.
        report = doctor(data_dir)
        assert report["running_config"]["settings"]["log_level"] == "ERROR"
        assert report["running_config"]["instance_id"] == launched["instance_id"]
    finally:
        stopped = stop(data_dir, timeout=15)
        if stopped["status"] != "stopped":
            for identity in (read_instance(data_dir) or {}).get("processes",{}).values():
                terminate_owned(identity,data_dir)
        assert status(data_dir)["status"] == "stopped"
