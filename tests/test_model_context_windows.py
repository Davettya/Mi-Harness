"""Application context tiers and startup migration contracts."""

from harness.context import ContextPolicy, input_budget
from harness.model_gateway import ModelLimits, ModelProfile, configured_model_limits, demo_profile
from harness.platform.config import Settings
from harness.runtime.models import AgentSpec
from harness.server.composition import DEFAULT_SYSTEM_PROMPT, Services


def test_default_and_explicit_extended_context_budgets():
    default = demo_profile()
    assert default.limits.context_window == 300_000
    assert default.limits.input_limit is None
    assert input_budget(default, ContextPolicy())["input_budget"] == 298_720

    extended = default.model_copy(update={"limits": configured_model_limits(1_000_000)})
    assert extended.limits.source_ref == "user-configured:model-context-window-1m-v1"
    assert input_budget(extended, ContextPolicy())["input_budget"] == 998_720


def test_startup_migrates_current_profile_and_preserves_historical_revision(tmp_path):
    data_dir = tmp_path / "data"
    initial = Services(Settings(data_dir=data_dir))
    legacy = demo_profile().model_copy(
        update={
            "profile_id": "legacy",
            "limits": ModelLimits(
                context_window=32_768,
                input_limit=30_000,
                output_limit=4_096,
                source_ref="legacy:test-limit",
            ),
        }
    )
    initial.store.put(
        "config/models",
        legacy.profile_id,
        {"id": legacy.profile_id, **legacy.model_dump(mode="json")},
    )
    initial.store.put("model_profiles", legacy.ref, legacy.model_dump(mode="json"))
    legacy_large = legacy.model_copy(
        update={
            "profile_id": "legacy-large",
            "limits": legacy.limits.model_copy(
                update={"context_window": 1_000_000, "input_limit": 900_000}
            ),
        }
    )
    initial.store.put(
        "config/models",
        legacy_large.profile_id,
        {"id": legacy_large.profile_id, **legacy_large.model_dump(mode="json")},
    )
    initial.store.put("model_profiles", legacy_large.ref, legacy_large.model_dump(mode="json"))
    initial.store.put(
        "model_endpoint_grants",
        legacy.ref,
        {
            "owner_id": "local",
            "profile_ref": legacy.ref,
            "endpoint": legacy.endpoint_ref,
            "enabled": True,
        },
    )
    agent = AgentSpec.model_validate(initial.store.get("config/agents", "default"))
    selected = agent.model_copy(
        update={
            "revision": agent.revision + 1,
            "model_policy": {
                "profile_ref": legacy.ref,
                "summary_profile_ref": legacy.ref,
            },
        }
    )
    initial.store.compare_and_set(
        "config/agents", "default", agent.revision, selected.model_dump(mode="json")
    )

    migrated_services = Services(Settings(data_dir=data_dir))
    current = ModelProfile.model_validate(
        {
            key: value
            for key, value in migrated_services.store.get("config/models", "legacy").items()
            if key not in {"id", "active"}
        }
    )
    assert current.revision == 2
    assert current.limits.context_window == 300_000
    assert current.limits.input_limit is None
    assert current.limits.output_limit == 4_096
    assert migrated_services.store.get("model_profiles", legacy.ref)["limits"][
        "context_window"
    ] == 32_768
    assert migrated_services.store.get("model_profiles", current.ref)
    large_current = migrated_services.store.get("config/models", "legacy-large")
    assert large_current["revision"] == 2
    assert large_current["limits"]["context_window"] == 1_000_000
    assert large_current["limits"]["input_limit"] is None
    copied_grant = migrated_services.store.get("model_endpoint_grants", current.ref)
    assert copied_grant["profile_ref"] == current.ref
    assert copied_grant["source"] == "model_context_default_migration"
    policy = migrated_services.store.get("config/agents", "default")["model_policy"]
    assert policy == {"profile_ref": current.ref, "summary_profile_ref": current.ref}

    Services(Settings(data_dir=data_dir))
    assert migrated_services.store.get("config/models", "legacy")["revision"] == 2


def test_startup_migrates_default_prompt_to_proportional_evidence_contract(tmp_path):
    data_dir = tmp_path / "data"
    initial = Services(Settings(data_dir=data_dir))
    initial.store.put("prompts", "builtin:default", {"text": "legacy prompt"})

    migrated = Services(Settings(data_dir=data_dir))
    assert migrated.store.get("prompts", "builtin:default") == {"text": DEFAULT_SYSTEM_PROMPT}
    assert "complete=true时立即作答" in DEFAULT_SYSTEM_PROMPT
    assert "不要再次读取文件或产物" in DEFAULT_SYSTEM_PROMPT
    assert "不要复述工具内部剪枝" in DEFAULT_SYSTEM_PROMPT

    revision_count = migrated.store._one(
        "SELECT revision FROM records WHERE namespace=? AND key=?",
        ("prompts", "builtin:default"),
    )["revision"]
    Services(Settings(data_dir=data_dir))
    assert migrated.store._one(
        "SELECT revision FROM records WHERE namespace=? AND key=?",
        ("prompts", "builtin:default"),
    )["revision"] == revision_count
