"""Model setup transport boundaries with real auth, SQLite and command transactions.

No listening socket or external provider is used. Only provider discovery/probes and
the credential backend are fixtures; saves exercise Services.save_config itself.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging

import pytest
from fastapi.testclient import TestClient

from harness.model_gateway.profiles import demo_profile
from harness.platform.config import PlatformConfig
from harness.server.app import create_app
from harness.server.composition import Services

ORIGIN = "http://127.0.0.1:18767"
CANARY = "sk-api-transport-canary-019e-only-in-request-memory"
CHANGED_CANARY = "sk-api-transport-canary-changed-019e"


class ProviderSetupFixture:
    def __init__(self, services):
        self.services = services
        self.calls = []
        self.fail = set()
        self.credentials = {}

    def providers(self):
        self.calls.append(("providers", None, {}))
        return {"items": [{"id": "openai", "name": "Fixture provider"}]}

    async def _probe(self, action, owner, body):
        self.calls.append((action, owner, copy.deepcopy(body)))
        await asyncio.sleep(0)
        if action in self.fail:
            raise RuntimeError("Synthetic provider failure containing " + body.get("api_key", ""))

    async def discover(self, owner, body):
        await self._probe("discover", owner, body)
        return {"items": [{"id": "fixture-model", "name": "Fixture model"}]}

    async def test(self, owner, body):
        await self._probe("test", owner, body)
        return {"status": "passed", "model_id": body["model_id"]}

    async def save(self, owner, body):
        await self._probe("save", owner, body)
        identity = body.get("profile_id") or "setup-fixture"
        previous = self.services.store.get("config/models", identity) or {}
        credential_ref = previous.get("credential_ref")
        if body.get("api_key"):
            credential_ref = f"credential:model-{identity}:{len(self.credentials):032x}"
            self.credentials[credential_ref] = body["api_key"]
        profile = demo_profile().model_dump(mode="json")
        profile.update(
            provider_id=body["provider_id"],
            model_id=body["model_id"],
            credential_ref=credential_ref,
            expected_revision=body.get("expected_revision"),
        )
        return self.services.save_config("models", identity, profile)


@pytest.fixture
def setup_api(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    services = Services(
        PlatformConfig(data_dir=tmp_path / "data", port=18767, instance_id="setup-api-fixture")
    )
    setup = ProviderSetupFixture(services)
    services.model_setup = setup
    mutations = []
    real_mutate = services.mutate

    async def observed_mutate(owner, method, route, key, body, callback):
        mutations.append(copy.deepcopy(body))
        return await real_mutate(owner, method, route, key, body, callback)

    services.mutate = observed_mutate
    app = create_app(services)
    with TestClient(app, base_url=ORIGIN, raise_server_exceptions=False) as client:
        yield client, app.state.auth, services, setup, mutations


def pair(client, auth):
    response = client.post(
        "/api/auth/exchange", json={"ticket": auth.issue_ticket()}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 204
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": client.cookies["harness_csrf"]})


def body(**overrides):
    return {"provider_id": "openai", "model_id": "fixture-model", "api_key": CANARY, **overrides}


def commands(services):
    return services.store._all("SELECT route,key,body_hash,response,status_code FROM api_commands")


def assert_secret_not_persisted(services, caplog, *responses):
    for secret in (CANARY, CHANGED_CANARY):
        assert secret not in caplog.text
        for response in responses:
            assert secret not in response.text
        assert secret not in json.dumps(commands(services))
        # Check SQLite, WAL and all other files, not just the current logical record view.
        for path in services.data_dir.rglob("*"):
            if path.is_file():
                assert secret.encode() not in path.read_bytes(), f"secret persisted in {path.name}"


def test_setup_routes_require_auth_host_origin_and_csrf(setup_api, caplog):
    client, auth, services, setup, _ = setup_api
    rejected = []
    assert client.get("/api/model-providers").status_code == 401
    for action in ("discover", "test", "save"):
        payload = body()
        if action == "discover":
            payload.pop("model_id")
        response = client.post(
            f"/api/model-setup/{action}",
            json=payload,
            headers={"Origin": ORIGIN, "Idempotency-Key": "unauth"},
        )
        assert response.status_code == 401
        rejected.append(response)
    pair(client, auth)
    assert client.get("/api/model-providers").is_success
    assert setup.calls == [("providers", None, {})]
    for headers, code in (
        ({"Host": "evil.invalid"}, "HOST_REJECTED"),
        ({"Origin": "https://evil.invalid"}, "ORIGIN_REJECTED"),
        ({"X-CSRF-Token": "incorrect"}, "CSRF_REJECTED"),
    ):
        response = client.post("/api/model-setup/test", json=body(), headers=headers)
        assert response.status_code == 403 and response.json()["code"] == code
        rejected.append(response)
    assert setup.calls == [("providers", None, {})]
    assert_secret_not_persisted(services, caplog, *rejected)


def test_model_setup_save_requires_idempotency_and_binds_secret_without_persisting_it(setup_api, caplog):
    client, auth, services, setup, mutations = setup_api
    pair(client, auth)
    assert client.post("/api/model-setup/save", json=body()).status_code == 400
    assert not setup.calls and not commands(services)
    headers = {"Idempotency-Key": "one-save"}
    first = client.post("/api/model-setup/save", json=body(), headers=headers)
    assert first.is_success, first.text
    repeated = client.post("/api/model-setup/save", json=body(), headers=headers)
    assert repeated.status_code == first.status_code and repeated.json() == first.json()
    changed = client.post("/api/model-setup/save", json=body(api_key=CHANGED_CANARY), headers=headers)
    assert changed.status_code == 409 and changed.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(setup.calls) == 1 and setup.calls[0][1] == "local"
    assert setup.calls[0][2]["api_key"] == CANARY
    assert len(commands(services)) == 1
    expected = hmac.new(
        client.cookies["harness_session"].encode(), CANARY.encode(), hashlib.sha256
    ).hexdigest()
    assert mutations[0]["api_key_fingerprint"] == expected
    assert "api_key" not in mutations[0]
    assert CANARY not in json.dumps(mutations)
    assert CHANGED_CANARY not in json.dumps(mutations)
    assert_secret_not_persisted(services, caplog, first, repeated, changed)


def test_discovery_and_test_keep_keys_ephemeral_and_do_not_write_commands(setup_api, caplog):
    client, auth, services, setup, mutations = setup_api
    pair(client, auth)
    responses = [
        client.post(
            "/api/model-setup/discover",
            json={"provider_id": "openai", "api_key": CANARY},
            headers={"Idempotency-Key": "probe-only"},
        ),
        client.post("/api/model-setup/test", json=body(), headers={"Idempotency-Key": "probe-only"}),
    ]
    assert all(response.is_success for response in responses), [r.text for r in responses]
    assert [call[0] for call in setup.calls] == ["discover", "test"]
    assert all(call[1] == "local" and call[2]["api_key"] == CANARY for call in setup.calls)
    assert not commands(services) and not mutations and not setup.credentials
    assert_secret_not_persisted(services, caplog, *responses)


def test_setup_validation_secretstr_and_provider_exceptions_do_not_echo_keys(setup_api, caplog):
    from harness.server.schemas import ModelDiscoveryInput, ModelSetupInput, ModelSetupSaveInput

    client, auth, services, setup, _ = setup_api
    for schema, payload in (
        (ModelDiscoveryInput, {"provider_id": "openai", "api_key": CANARY}),
        (ModelSetupInput, body()),
        (ModelSetupSaveInput, body()),
    ):
        value = schema.model_validate(payload)
        assert value.api_key.get_secret_value() == CANARY
        assert CANARY not in repr(value) and CANARY not in value.model_dump_json()
    pair(client, auth)
    invalid = client.post("/api/model-setup/test", json={"provider_id": "openai", "api_key": CANARY})
    assert invalid.status_code == 422 and not setup.calls
    invalid_type = client.post("/api/model-setup/test", json=body(api_key=[CANARY]))
    assert invalid_type.status_code == 422 and not setup.calls
    incomplete_edits = []
    for partial in ({"profile_id": "setup-fixture"}, {"expected_revision": 1}):
        response = client.post(
            "/api/model-setup/save", json=body(**partial), headers={"Idempotency-Key": "invalid-edit"}
        )
        assert response.status_code == 422 and not setup.calls
        incomplete_edits.append(response)
    setup.fail.update({"discover", "test", "save"})
    failures = []
    for action in ("discover", "test", "save"):
        payload = body()
        if action == "discover":
            payload.pop("model_id")
        response = client.post(
            f"/api/model-setup/{action}", json=payload, headers={"Idempotency-Key": "failure-" + action}
        )
        assert response.status_code >= 400
        failures.append(response)
    assert len(commands(services)) == 1  # Async save failure is cached safely; probes are ephemeral.
    repeated = client.post("/api/model-setup/save", json=body(), headers={"Idempotency-Key": "failure-save"})
    assert repeated.json()["code"] == failures[-1].json()["code"]
    assert len([call for call in setup.calls if call[0] == "save"]) == 1
    assert_secret_not_persisted(
        services, caplog, invalid, invalid_type, *incomplete_edits, *failures, repeated
    )


def test_maintenance_blocks_every_model_setup_post_including_cached_save(setup_api, caplog):
    client, auth, services, setup, _ = setup_api
    pair(client, auth)
    first = client.post(
        "/api/model-setup/save", json=body(), headers={"Idempotency-Key": "before-maintenance"}
    )
    assert first.is_success, first.text
    baseline = len(setup.calls)
    services.store.put("system", "maintenance", {"enabled": True, "action": "backup"})
    responses = []
    for action in ("discover", "test", "save"):
        payload = body()
        if action == "discover":
            payload.pop("model_id")
        response = client.post(
            f"/api/model-setup/{action}", json=payload, headers={"Idempotency-Key": "maintenance-" + action}
        )
        assert response.status_code == 503 and response.json()["code"] == "MAINTENANCE"
        responses.append(response)
    replay = client.post(
        "/api/model-setup/save", json=body(), headers={"Idempotency-Key": "before-maintenance"}
    )
    assert replay.status_code == 503
    assert len(setup.calls) == baseline and len(commands(services)) == 1
    assert_secret_not_persisted(services, caplog, first, *responses, replay)


def test_existing_profile_empty_key_preserves_vault_reference_and_expected_revision(setup_api, caplog):
    client, auth, services, setup, _ = setup_api
    pair(client, auth)
    created = client.post("/api/model-setup/save", json=body(), headers={"Idempotency-Key": "create"})
    assert created.is_success, created.text
    original = services.store.get("config/models", "setup-fixture")
    update = body(profile_id="setup-fixture", api_key="", model_id="fixture-model-2", expected_revision=1)
    changed = client.post("/api/model-setup/save", json=update, headers={"Idempotency-Key": "update"})
    assert changed.is_success, changed.text
    current = services.store.get("config/models", "setup-fixture")
    assert current["revision"] == 2 and current["model_id"] == "fixture-model-2"
    assert current["credential_ref"] == original["credential_ref"] and len(setup.credentials) == 1
    assert setup.calls[-1][2]["expected_revision"] == 1
    assert not setup.calls[-1][2].get("api_key")
    stale = client.post("/api/model-setup/save", json=update, headers={"Idempotency-Key": "stale-update"})
    assert stale.status_code == 409 and stale.json()["code"] == "REVISION_CONFLICT"
    assert services.store.get("config/models", "setup-fixture") == current
    assert_secret_not_persisted(services, caplog, created, changed, stale)
