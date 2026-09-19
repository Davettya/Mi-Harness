"""Default admission, retry and partial-evidence regression tests through real SDK probes."""

import pytest

from harness.core import HarnessError
from test_model_setup import services, model_server  # noqa: F401


@pytest.mark.parametrize("optional_checks", [None, [], ["vision"], ["streaming", "streaming"]])
async def test_direct_save_cannot_skip_full_suite(services, model_server, optional_checks):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    if optional_checks is not None:
        body["optional_checks"] = optional_checks
    saved = await services.model_setup.save("local", body)
    assert len(calls) == 5
    assert set(saved["profile"]["capabilities"]) == {"text", "tool_calling", "vision", "streaming"}
    await services.close()


async def test_partial_failure_preserves_profile_and_requires_matching_receipt(services, model_server):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    original = await services.model_setup.save("local", body)
    agent = services.store.get("config/agents", "default")
    edit = {**body, "profile_id": original["id"], "expected_revision": 1, "base_url": url + "/blind"}
    for accept in ([], ["vision"]):
        with pytest.raises(HarnessError) as exc:
            await services.model_setup.save("local", {**edit, "accept_unverified_capabilities": accept})
        assert exc.value.code == "MODEL_CAPABILITIES_INCOMPLETE"
        assert services.store.get("config/models", original["id"])["revision"] == 1
        assert services.store.get("config/agents", "default") == agent
    result = await services.model_setup.test("local", edit)
    assert result["reused"] and result["checks"]["vision"] is False
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save(
            "local",
            {
                **edit,
                "verification_token": result["verification_token"],
                "accept_unverified_capabilities": ["streaming"],
            },
        )
    assert exc.value.code == "MODEL_CAPABILITIES_INCOMPLETE"
    before = len(calls)
    retry = await services.model_setup.test("local", {**edit, "refresh_verification": True})
    assert not retry["reused"] and len(calls) - before == 5
    assert retry["verification_token"] != result["verification_token"]
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save(
            "local",
            {
                **edit,
                "verification_token": result["verification_token"],
                "accept_unverified_capabilities": ["vision"],
            },
        )
    assert exc.value.code == "MODEL_VERIFICATION_EXPIRED"
    saved = await services.model_setup.save(
        "local",
        {
            **edit,
            "verification_token": retry["verification_token"],
            "accept_unverified_capabilities": ["vision"],
        },
    )
    assert saved["revision"] == 2 and "vision" not in saved["profile"]["capabilities"]
    assert services.models.get_profile(original["id"] + "@1").supports("vision")
    report = services.store.get("model_verifications", saved["profile"]["verification_ref"])
    assert report["accepted_unverified_capabilities"] == ["vision"]
    assert len(calls) - before == 5  # Matching save does not send a sixth call.
    await services.close()


async def test_quick_activation_and_draft_overwrite_cannot_change_working_model(services, model_server):
    url, calls = model_server
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    quick = await services.model_setup.test(
        "local", {**body, "probe_mode": "connectivity", "optional_checks": ["vision"]}
    )
    assert len(calls) == 1 and quick["optional_checks"] == []
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save("local", {**body, "probe_mode": "connectivity"})
    assert exc.value.code == "MODEL_FULL_VERIFICATION_REQUIRED" and len(calls) == 1
    saved = await services.model_setup.save("local", body)
    assert len(calls) == 6
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save(
            "local", {**body, "profile_id": saved["id"], "expected_revision": 1, "activate": False}
        )
    assert exc.value.code == "MODEL_VERIFIED_DRAFT"
    assert services.store.get("config/models", saved["id"])["revision"] == 1
    await services.close()


async def test_disabled_reuse_still_rejects_stale_or_changed_receipt(services, model_server):
    url, calls = model_server
    services.settings = services.settings.model_copy(update={"verification_reuse": False})
    body = dict(provider_id="custom", base_url=url + "/v1", model_id="fixture-model")
    result = await services.model_setup.test("local", body)
    with pytest.raises(HarnessError) as exc:
        await services.model_setup.save(
            "local", {**body, "model_id": "changed", "verification_token": result["verification_token"]}
        )
    assert exc.value.code == "MODEL_VERIFICATION_EXPIRED" and len(calls) == 5
    saved = await services.model_setup.save(
        "local", {**body, "verification_token": result["verification_token"]}
    )
    assert not saved["verification_reused"] and len(calls) == 10
    await services.close()
