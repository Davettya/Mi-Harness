import asyncio

import pytest

from harness.core import HarnessError
from test_model_setup import services, model_server  # noqa: F401


async def test_full_vision_and_streaming_probe_has_five_calls_and_immutable_evidence(services, model_server):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    legacy_body = {**body, "base_url": url + "/blind"}
    legacy_test = await services.model_setup.test("local", legacy_body)
    original = await services.model_setup.save(
        "local",
        {
            **legacy_body,
            "verification_token": legacy_test["verification_token"],
            "accept_unverified_capabilities": ["vision"],
        },
    )
    old_ref = original["id"] + "@1"
    with pytest.raises(HarnessError) as exc:
        services.selection.validate("local", old_ref, images=True)
    assert exc.value.code == "VISION_UNVERIFIED"
    request = {**body, "profile_id": original["id"], "optional_checks": ["vision", "streaming"]}
    before = len(calls)
    result = await services.model_setup.test("local", request)
    assert result["checks"]["vision"] is True
    assert result["checks"]["streaming"] is True
    assert result["request_count"] == 5 and len(calls) - before == 5
    assert result["check_failures"] == {}
    saved = await services.model_setup.save(
        "local", {**request, "expected_revision": 1, "verification_token": result["verification_token"]}
    )
    assert saved["verification_reused"] and len(calls) - before == 5
    profile = services.selection.validate("local", saved["id"] + "@2", images=True)
    assert profile.supports("vision") and profile.supports("streaming")
    assert not services.models.get_profile(old_ref).supports("vision")
    report = services.store.get("model_verifications", profile.verification_ref)
    assert {"vision", "streaming"}.issubset(report["requested_checks"])
    await services.close()


async def test_failed_visual_answer_remains_unverified_not_unsupported(services, model_server):
    url, _ = model_server
    body = dict(
        provider_id="custom", base_url=url + "/blind", model_id="fixture-model", optional_checks=["vision"]
    )
    result = await services.model_setup.test("local", body)
    assert result["checks"]["vision"] is False
    assert result["check_failures"]["vision"] == "PROBE_RESPONSE_MISMATCH"
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save("local", {**body, "verification_token": result["verification_token"]})
    assert exc.value.code == "MODEL_CAPABILITIES_INCOMPLETE"
    saved = await services.model_setup.save(
        "local",
        {
            **body,
            "verification_token": result["verification_token"],
            "accept_unverified_capabilities": ["vision"],
        },
    )
    assert "vision" not in saved["profile"]["capabilities"]
    await services.close()


async def test_connectivity_draft_and_credential_failure_do_not_activate(services, model_server, monkeypatch):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model", api_key="canary")
    initial = services.store.get("config/agents", "default")
    quick = await services.model_setup.test("local", {**body, "probe_mode": "connectivity"})
    assert quick["connected"] and not quick["tool_calling"] and len(calls) == 1
    with pytest.raises(HarnessError):
        await services.model_setup.save("local", {**body, "verification_token": quick["verification_token"]})
    draft = await services.model_setup.save("local", {**body, "activate": False})
    assert not draft["active"] and not draft["profile"]["capabilities"] and len(calls) == 1
    assert services.store.get("config/agents", "default") == initial
    verified = await services.model_setup.test("local", body)
    before = services.store.list("config/models")

    def fail(*args):
        raise RuntimeError("vault unavailable")

    monkeypatch.setattr(services.vault, "put", fail)
    with pytest.raises(HarnessError) as error:
        await services.model_setup.save(
            "local", {**body, "verification_token": verified["verification_token"]}
        )
    assert error.value.code == "MODEL_CREDENTIAL_STORAGE_FAILED"
    assert services.store.list("config/models") == before
    assert services.store.get("config/agents", "default") == initial
    await services.close()


async def test_cancelled_last_waiter_cancels_probe_without_receipt(services, model_server, monkeypatch):
    url, _ = model_server
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def stalled(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(services.model_setup, "_probe", stalled)
    task = asyncio.create_task(
        services.model_setup.test("local", dict(provider_id="custom", base_url=url, model_id="fixture-model"))
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not services.model_setup._flights and not services.model_setup._cache
    await services.close()


async def test_validation_receipt_reuse_and_invalidation(services, model_server):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model", api_key="canary")
    result = await services.model_setup.test("local", body)
    assert len(calls) == 5
    saved = await services.model_setup.save(
        "local", {**body, "verification_token": result["verification_token"]}
    )
    assert saved["verification_reused"] and len(calls) == 5
    for change in ({"model_id": "changed"}, {"api_key": "changed"}, {"base_url": url + "/other"}):
        with pytest.raises(HarnessError) as exc:
            await services.model_setup.save(
                "local", {**body, **change, "verification_token": result["verification_token"]}
            )
        assert exc.value.code == "MODEL_VERIFICATION_EXPIRED"
    assert len(calls) == 5
    await services.close()


async def test_single_flight_owner_scope_and_expiry(services, model_server):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    a, b = await asyncio.gather(
        services.model_setup.test("local", body), services.model_setup.test("local", body)
    )
    assert a["verification_token"] == b["verification_token"] and len(calls) == 5
    with pytest.raises(HarnessError):
        await services.model_setup.save("other", {**body, "verification_token": a["verification_token"]})
    services.model_setup._cache.clear()
    with pytest.raises(HarnessError):
        await services.model_setup.save("local", {**body, "verification_token": a["verification_token"]})
    assert len(calls) == 5
    await services.close()
