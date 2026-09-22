import json
import pytest

from harness.core import HarnessError, OperationKey
from harness.policy.engine import DEFAULT_POLICY
from harness.runtime.models import AgentSpec
from test_integration import setup_services, submit


def test_named_policy_and_default_context_are_resolved_and_unknown_refs_rejected(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    services.save_config("policies","reader",{**DEFAULT_POLICY,"expected_revision":None,
        "capabilities":["file_read"],"egress":"local_only"})
    agent = services.store.get("config/agents","default")
    services.store.put("config/agents", "default", {**agent, "revision": agent["revision"] + 1, "policy": "reader"})
    run_id = submit(services,session,"read file")
    ctx = services.scheduler.claim("fixture")
    snapshot = services.store.get("snapshots",services.store.run(run_id)["snapshot_id"])
    assert snapshot["policy"]["capabilities"]==["file_read"]
    assert snapshot["policy"]["egress"]=="local_only"
    assert snapshot["context_policy"]["revision"]==3
    assert snapshot["context_policy"]["output_reserve"]==8192
    assert services.policy.effective(ctx)["capabilities"]==["file_read"]
    revision = services.policy.effective(ctx)["revision"]
    services.save_config("policies","reader",{**DEFAULT_POLICY,"expected_revision":1,"capabilities":[]})
    assert services.policy.effective(ctx)["capabilities"]==[]
    assert services.policy.effective(ctx)["revision"]!=revision
    assert services.policy.effective(ctx)["egress"]=="local_only"  # Frozen run ceiling survives current widening.
    agent = services.store.get("config/agents","default")
    for field in ("policy","context_policy"):
        with pytest.raises(HarnessError) as error:
            services.validate_agent_policies(AgentSpec.model_validate({**agent, field: "missing"}))
        assert error.value.status==422
        assert services.store.get("config/agents","bad-agent") is None


@pytest.mark.asyncio
async def test_skill_current_source_revocation_and_binary_resource_gateway_artifact(tmp_path):
    services, workspace, session = setup_services(tmp_path)
    root = tmp_path / "skill-source"
    root.mkdir()
    (root / "SKILL.md").write_text("---\nname: picture\ndescription: fixture binary asset\n---\nRead assets only on request.",encoding="utf-8")
    from io import BytesIO
    from PIL import Image
    png = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(png, format="PNG")
    payload = png.getvalue()
    (root / "asset.png").write_bytes(payload)
    source = services.save_config("skill_sources","fixture",{"expected_revision":None,"root":str(root),"scope":"user","trusted":False,"enabled":True})
    services.dispatch("refresh_skills","local",body={"source_ids":["fixture"]})
    assert next(s for s in services.store.list("skills") if s["source_id"]=="fixture")["trust_state"]=="unreviewed"
    services.save_config("skill_sources","fixture",{**source,"expected_revision":source["revision"],"trusted":True})
    services.dispatch("refresh_skills","local",body={"source_ids":["fixture"]})
    skill = next(s for s in services.store.list("skills") if s["source_id"]=="fixture")
    agent = services.store.get("config/agents","default")
    services.store.put("config/agents", "default", {**agent, "revision": agent["revision"] + 1, "skills": [skill["skill_id"]]})
    run_id = submit(services,session,"read a skill asset")
    ctx = services.scheduler.claim("fixture")
    await services.prepare_run(ctx)
    activation = services.skill_activator.active(ctx)[0]
    result = await services.gateway.execute(ctx,OperationKey(run_id=run_id,message_id="fixture",tool_call_id="binary"),
        "skill_read",{"activation_id":activation.activation_ref,"resource":"asset.png"})
    assert result.status=="succeeded" and len(result.artifact_refs)==1
    ref = result.artifact_refs[0]
    assert services.artifacts.path(ref.artifact_id,"local").read_bytes()==payload
    assert result.structured_data["mime_type"]=="image/png"
    json.dumps(result.model_dump(mode="json"))
    for change in ({"trusted":False,"enabled":True},{"trusted":True,"enabled":False}):
        config = services.store.get("config/skill_sources","fixture")
        services.save_config("skill_sources","fixture",{**config,"expected_revision":config["revision"],**change})
        with pytest.raises(HarnessError) as error:
            services.skill_activator.read_resource(activation.activation_ref,"asset.png",ctx)
        assert error.value.code=="SKILL_SOURCE_REVOKED"
    await services.close()
