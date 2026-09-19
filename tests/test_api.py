from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from harness.artifacts.store import ArtifactStore
from harness.core import HarnessError, content_hash
from harness.server.app import create_app
from harness.storage import Store


class TestServices:
    __test__ = False

    def __init__(self, tmp_path):
        self.settings = SimpleNamespace(host="127.0.0.1", port=8767, instance_id="test-instance")
        self.store = Store(tmp_path / "state.sqlite3")
        self.artifacts = ArtifactStore(self.store, tmp_path / "artifacts", max_bytes=1024)
        self.capabilities = {"memory": True, "branches": True, "steering": True, "children": True, "mcp_oauth": False}
        self.calls = []

    def dispatch(self, action, owner, **kwargs):
        self.calls.append((action, owner, kwargs))
        if action == "create_workspace":
            return self.store.create_workspace(owner, kwargs["body"]["name"], kwargs["body"]["root_candidate"], {})
        if action == "list_workspaces":
            return {"items": self.store.workspaces(owner), "next_cursor": None}
        if action == "get_run":
            return {"run_id": kwargs["run_id"], "status": "queued", "revision": 1, "last_seq": 0}
        return {"id": "test", "revision": 2, "status": "queued"}

    def mutate(self, owner, method, route, key, body, callback):
        with self.store.transaction():
            previous = self.store.api_command(owner, method, route, key)
            if previous:
                if previous["body_hash"] != content_hash(body):
                    raise HarnessError("IDEMPOTENCY_CONFLICT", "幂等正文不同")
                return previous["response"]
            value = callback()
            self.store.save_api_command(owner, method, route, key, content_hash(body), value, 200, (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
            return value


@pytest.fixture
def api(tmp_path):
    services = TestServices(tmp_path)
    app = create_app(services)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8767",
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
    ) as client:
        yield client, app.state.auth, services


def pair(client, auth):
    ticket = auth.issue_ticket()
    response = client.post("/api/auth/exchange", json={"ticket": ticket}, headers={"Origin": "http://127.0.0.1:8767"})
    assert response.status_code == 204
    client.headers.update({"Origin": "http://127.0.0.1:8767", "X-CSRF-Token": client.cookies["harness_csrf"], "Idempotency-Key": "test-command"})
    return ticket


def test_pairing_single_use_expiry_cookie_and_logout(api):
    client, auth, _ = api
    assert client.get("/api/workspaces").status_code == 401
    expired = auth.issue_ticket(ttl_seconds=-1)
    assert client.post("/api/auth/exchange", json={"ticket": expired}, headers={"Origin": "http://127.0.0.1:8767"}).status_code == 401
    ticket = pair(client, auth)
    assert client.get("/api/auth/session").json()["owner_id"] == "local"
    assert client.post("/api/auth/exchange", json={"ticket": ticket}).status_code == 401
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/session").status_code == 401


def test_loopback_same_origin_pairing_is_silent_but_apis_remain_authenticated(api):
    client, _, services = api
    assert client.get("/api/workspaces").status_code == 401
    denied = client.post(
        "/api/auth/local",
        headers={"Origin": "https://evil.invalid"},
    )
    assert denied.status_code == 403 and denied.json()["code"] == "ORIGIN_REJECTED"
    response = client.post(
        "/api/auth/local",
        headers={"Origin": "http://127.0.0.1:8767"},
    )
    assert response.status_code == 204
    assert client.cookies["harness_session"] and client.cookies["harness_csrf"]
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert all("SameSite=strict" in value for value in response.headers.get_list("set-cookie"))
    session = client.get("/api/auth/session")
    assert session.status_code == 200 and session.json()["owner_id"] == "local"
    client.headers.update(
        {
            "Origin": "http://127.0.0.1:8767",
            "X-CSRF-Token": client.cookies["harness_csrf"],
            "Idempotency-Key": "automatic-session-command",
        }
    )
    created = client.post(
        "/api/workspaces",
        json={"name": "automatic", "root_candidate": "C:/automatic"},
    )
    assert created.status_code == 201
    sessions = services.store.list("auth_sessions")
    assert len(sessions) == 1 and sessions[0]["source"] == "loopback_auto"


def test_automatic_pairing_rejects_non_loopback_peer(tmp_path):
    services = TestServices(tmp_path)
    app = create_app(services)
    with TestClient(
        app,
        base_url="http://127.0.0.1:8767",
        raise_server_exceptions=False,
        client=("192.0.2.20", 50000),
    ) as client:
        response = client.post(
            "/api/auth/local",
            headers={"Origin": "http://127.0.0.1:8767"},
        )
    assert response.status_code == 403
    assert response.json()["code"] == "LOCAL_PAIRING_DENIED"
    assert not services.store.list("auth_sessions")


def test_origin_host_csrf_are_independent_guards(api):
    client, auth, _ = api
    pair(client, auth)
    body = {"name": "work", "root_candidate": "C:/work"}
    assert client.post("/api/workspaces", json=body, headers={"Origin": "https://evil.invalid"}).json()["code"] == "ORIGIN_REJECTED"
    assert client.post("/api/workspaces", json=body, headers={"Host": "evil.invalid"}).json()["code"] == "HOST_REJECTED"
    assert client.post("/api/workspaces", json=body, headers={"X-CSRF-Token": "invalid"}).json()["code"] == "CSRF_REJECTED"
    assert client.post("/api/workspaces", json=body).status_code == 201


def test_idempotent_replay_precedes_business_and_conflicting_body_is_rejected(api):
    client, auth, services = api
    pair(client, auth)
    body = {"name": "work", "root_candidate": "C:/work"}
    first = client.post("/api/workspaces", json=body)
    assert client.post("/api/workspaces", json=body).json() == first.json()
    assert len(services.store.workspaces("local")) == 1
    response = client.post("/api/workspaces", json={**body, "name": "different"})
    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert len([call for call in services.calls if call[0] == "create_workspace"]) == 1


def test_upload_authorization_size_dedup_and_active_content_isolation(api):
    client, auth, services = api
    pair(client, auth)
    workspace = services.store.create_workspace("local", "work", "C:/work", {})
    other = services.store.create_workspace("other", "other", "C:/other", {})
    response = client.post("/api/artifacts", data={"workspace_id": other["id"]}, files={"file": ("secret.txt", b"secret", "text/plain")})
    assert response.status_code == 404
    response = client.post("/api/artifacts", data={"workspace_id": workspace["id"]}, files={"file": ("big.txt", b"a" * 1025, "text/plain")})
    assert response.status_code == 413
    data = b"<script>document.cookie</script>"
    def upload():
        return client.post("/api/artifacts", data={"workspace_id": workspace["id"]}, files={"file": ("proof.html", data, "text/html")})
    result = upload()
    assert result.status_code == 201, result.text
    assert upload().json() == result.json()
    response = client.get(f"/api/artifacts/{result.json()['artifact_id']}?disposition=preview")
    assert response.content == data
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_schema_validation_does_not_echo_private_input(api):
    client, auth, _ = api
    pair(client, auth)
    response = client.post("/api/workspaces", json={"name": "x", "root_candidate": "x", "unknown": "canary-secret"})
    assert response.status_code == 422
    assert "canary-secret" not in response.text
    schema = client.get("/openapi.json").json()
    assert "SubmitRunInput" in schema["components"]["schemas"]
    assert "MessageView" in schema["components"]["schemas"]
    assert "/api/runs/{run_id}/events" in schema["paths"]


def test_sse_rejects_future_conflicting_and_wrong_run_cursors_before_stream(api):
    client, auth, services = api
    pair(client, auth)
    services.store.run = lambda run_id, owner=None: {"id": run_id, "owner_id": owner}
    services.store.event_range = lambda run_id: (3, 8)
    assert client.get("/api/runs/r/events?after_seq=9").json()["code"] == "INVALID_CURSOR"
    assert client.get("/api/runs/r/events?after_seq=0").json()["code"] == "CURSOR_EXPIRED"
    assert client.get("/api/runs/r/events", headers={"Last-Event-ID": "other:5"}).status_code == 422
    assert client.get("/api/runs/r/events?after_seq=3", headers={"Last-Event-ID": "r:4"}).status_code == 400


def test_feature_unavailable_is_explicit(api):
    client, auth, _ = api
    pair(client, auth)
    response = client.post("/api/mcp/remote/authorize", json={})
    assert response.status_code == 501
    assert response.json()["code"] == "FEATURE_NOT_ENABLED"


def test_static_plugin_routes_use_typed_manifest_and_idempotency(api):
    client, auth, services = api
    pair(client, auth)
    body = {"manifest": {"id": "ui-smoke", "version": "1.0.0", "harness_api": ">=1,<2", "contributes": {}}}
    first = client.post("/api/plugins", json=body)
    assert first.status_code == 201
    assert client.post("/api/plugins", json=body).json() == first.json()
    calls = [call for call in services.calls if call[0] == "load_plugin"]
    assert len(calls) == 1
    assert calls[0][2]["body"]["manifest"]["id"] == "ui-smoke"
    assert client.post("/api/plugins", json={"manifest": {**body["manifest"], "contributes": {"unknown": []}}}).status_code == 422
    response = client.post("/api/plugins/ui-smoke/versions/1.0.0/drain", json={})
    assert response.status_code == 202
    assert services.calls[-1][2] == {"plugin_id": "ui-smoke", "version": "1.0.0", "body": {}}


def test_public_message_schema_accepts_empty_tool_call_assistant_text():
    from harness.server.schemas import MessageView
    message = MessageView(message_id="tool-call", role="assistant", content_parts=[{"type": "text", "text": ""}])
    assert message.content_parts[0].text == ""
