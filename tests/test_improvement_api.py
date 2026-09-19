from fastapi.testclient import TestClient

from test_improvement_controls import second_model
from test_integration import setup_services, submit

from harness.model_gateway.profiles import demo_profile
from harness.server.app import create_app


def test_control_auth_cas_idempotency_and_readonly_transport(tmp_path):
    services, _, session = setup_services(tmp_path)
    second_model(services)
    app = create_app(services)
    base = "http://127.0.0.1:8767"
    with TestClient(app, base_url=base) as client:
        assert client.get("/api/models/available").status_code == 401
        client.post(
            "/api/auth/exchange", json={"ticket": app.state.auth.issue_ticket()}, headers={"Origin": base}
        )
        client.headers.update({"Origin": base, "X-CSRF-Token": client.cookies["harness_csrf"]})
        assert len(client.get("/api/agents").json()["items"]) == 1
        response = client.put(
            "/api/config/agents/default", json={"expected_revision": 1}, headers={"Idempotency-Key": "agent"}
        )
        assert response.status_code == 403 and response.json()["code"] == "AGENT_READONLY"
        rid = submit(services, session, "hello")
        route = f"/api/runs/{rid}/model-selection"
        body = dict(
            model_profile_ref="second@1",
            expected_control_revision=0,
            persist_for_session=True,
            expected_preferences_revision=1,
        )
        assert client.post(route, json=body).status_code == 400
        first = client.post(route, json=body, headers={"Idempotency-Key": "switch"})
        assert first.status_code == 202, first.text
        duplicate = client.post(route, json=body, headers={"Idempotency-Key": "switch"})
        assert duplicate.json() == first.json()
        changed = client.post(
            route, json={**body, "model_profile_ref": "demo@1"}, headers={"Idempotency-Key": "switch"}
        )
        assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
        conflict = client.post(route, json=body, headers={"Idempotency-Key": "tab-two"})
        assert conflict.status_code == 409 and len(services.store.list("run_model_commands")) == 1
        snapshot = client.get(f"/api/runs/{rid}").json()
        assert snapshot["model_control"]["desired_profile_ref"] == "second@1"
        assert snapshot["model_control"]["effective_profile_ref"] == "demo@1"
        assert snapshot["mode"] == "react"
        response = client.post(
            "/api/mcp-config/validate", json={"text": '{"schema_version":1,"mcpServers":{}}'}
        )
        assert response.json()["connected"] is False
        file = client.get("/api/mcp-config/file").json()
        conflict = client.put(
            "/api/mcp-config/file",
            json={"text": file["text"], "expected_hash": "0" * 64},
            headers={"Idempotency-Key": "file-conflict"},
        )
        assert conflict.status_code == 412


def test_product_model_catalog_excludes_internal_demo(tmp_path):
    services, _, _ = setup_services(tmp_path)
    profile = demo_profile().model_copy(
        update={
            "profile_id": "model-deepseek",
            "provider_id": "deepseek",
            "adapter_id": "openai",
            "endpoint_ref": "https://api.deepseek.com/v1",
            "model_id": "deepseek-flash",
            "api_mode": "openai_chat_completions",
        }
    )
    services.models.register(profile)
    services.store.put(
        "config/models",
        profile.profile_id,
        {"id": profile.profile_id, **profile.model_dump(mode="json")},
    )
    app = create_app(services)
    base = "http://127.0.0.1:8767"
    with TestClient(app, base_url=base) as client:
        client.post(
            "/api/auth/exchange",
            json={"ticket": app.state.auth.issue_ticket()},
            headers={"Origin": base},
        )
        client.headers.update(
            {"Origin": base, "X-CSRF-Token": client.cookies["harness_csrf"]}
        )
        catalog = client.get("/api/models/available").json()
        assert [item["model_id"] for item in catalog["items"]] == ["deepseek-flash"]
        assert catalog["default_profile_ref"] == profile.ref
        assert catalog["pinned_profile_ref"] is None

        configured = client.get("/api/config/models").json()
        assert [item["model_id"] for item in configured["items"]] == ["deepseek-flash"]
        missing = client.get("/api/config/models/demo")
        assert missing.status_code == 404 and missing.json()["code"] == "MODEL_NOT_FOUND"
