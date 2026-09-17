from __future__ import annotations

import hashlib
import mimetypes
import uuid
from .models import ActivatedSkill, ResourceContent, SkillActivation, SkillRecord, utcnow
from .parser import parse_skill


class SkillActivator:
    def __init__(self, registry):
        self.registry, self.repository = registry, registry.repository

    def activate(self, skill_id: str, execution_context, *, content_hash: str | None = None,
                 activated_by="user", reason="explicit activation", agent_id="default", allowlist=None):
        if not getattr(execution_context, "run_id", None):
            raise PermissionError("skill activation requires ExecutionContext")
        if allowlist is not None and skill_id not in allowlist:
            raise PermissionError("skill is outside AgentSpec allowlist")
        with self.registry._lock:
            current = self.registry.get(skill_id)
            # A run's first activation pins the version even if source updates later.
            existing = next((a for a in self.repository.list("skill_activations")
                             if a["run_id"] == execution_context.run_id and a["agent_id"] == agent_id
                             and a["skill_id"] == skill_id and not a.get("deactivated_at")), None)
            digest = content_hash or (existing["content_hash"] if existing else current.content_hash)
            if existing and digest != existing["content_hash"]:
                raise ValueError("active skill version migration requires explicit deactivation")
            version = self.repository.get("skill_versions", skill_id + ":" + digest)
            if not version:
                raise FileNotFoundError("pinned skill version not available")
            record = SkillRecord.model_validate(version)
            # Current revocations apply, even for existing pinned activations.
            if not current.enabled or current.trust_state == "blocked" or current.validation_diagnostics:
                raise PermissionError("skill currently disabled, blocked, or invalid")
            self.registry.authorize("activate", execution_context, current)
            self.registry.authorize("read", execution_context, record)
            body = self.registry.snapshots.read(digest, "SKILL.md").decode("utf-8")
            _, body, diagnostics = parse_skill(body)
            if diagnostics:
                raise ValueError("pinned skill snapshot has invalid frontmatter")
            if existing:
                activation = SkillActivation.model_validate(existing)
                self._check_owner(activation, execution_context)
            else:
                activation = SkillActivation(activation_ref=str(uuid.uuid4()), owner_id=execution_context.owner_id,
                    workspace_id=execution_context.workspace_id, session_id=execution_context.session_id,
                    run_id=execution_context.run_id, agent_id=agent_id, skill_id=skill_id, content_hash=digest,
                    activated_by=activated_by, reason=reason)
                self.repository.put("skill_activations", activation.activation_ref, activation.model_dump(mode="json"))
            return ActivatedSkill(activation=activation, record=record, body=body, snapshot_ref=digest,
                                  resources=sorted(self.registry.snapshots.manifest(digest))[:256])

    @staticmethod
    def _check_owner(activation, context):
        if (activation.owner_id, activation.workspace_id, activation.run_id) != (
                context.owner_id, context.workspace_id, context.run_id):
            raise PermissionError("skill activation belongs to another execution scope")

    def read_resource(self, activation_ref: str, resource_ref: str, execution_context):
        with self.registry._lock:
            value = self.repository.get("skill_activations", activation_ref)
            if value is None:
                raise KeyError("unknown activation")
            activation = SkillActivation.model_validate(value)
            self._check_owner(activation, execution_context)
            if activation.deactivated_at:
                raise PermissionError("skill activation ended")
            record = self.registry.get(activation.skill_id)
            if not record.enabled or record.trust_state == "blocked":
                raise PermissionError("skill access revoked")
            self.registry.authorize("read", execution_context, record)
            content = self.registry.snapshots.read(activation.content_hash, resource_ref)
            if resource_ref not in activation.loaded_resource_refs:
                activation.loaded_resource_refs.append(resource_ref)
                self.repository.put("skill_activations", activation_ref, activation.model_dump(mode="json"))
            return ResourceContent(content=content, mime_type=mimetypes.guess_type(resource_ref)[0] or "application/octet-stream",
                source_uri=record.root_uri + "/" + resource_ref, content_hash=hashlib.sha256(content).hexdigest(),
                snapshot_ref=activation.content_hash)

    def active(self, execution_context):
        return [SkillActivation.model_validate(a) for a in self.repository.list("skill_activations")
                if a["run_id"] == execution_context.run_id and a["owner_id"] == execution_context.owner_id
                and a["workspace_id"] == execution_context.workspace_id and not a.get("deactivated_at")]

    def deactivate(self, activation_ref: str, reason: str, execution_context):
        with self.registry._lock:
            activation = SkillActivation.model_validate(self.repository.get("skill_activations", activation_ref))
            self._check_owner(activation, execution_context)
            activation.deactivated_at = utcnow()
            data = activation.model_dump(mode="json")
            data["deactivation_reason"] = reason
            self.repository.put("skill_activations", activation_ref, data)
            return activation
