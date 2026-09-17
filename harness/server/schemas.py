"""Transport schemas owned by implementation/10; domain records retain their owners."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from harness.core import ApprovalBinding, ArtifactRef, CheckpointRef, DTO, RunStatus
from harness.plugins.manifest import PluginManifest


class Page(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None = None


class PairingInput(DTO):
    ticket: str = Field(min_length=1, max_length=512)


class AuthSessionView(DTO):
    owner_id: str
    csrf_token: str
    capabilities: dict[str, bool]


class WorkspaceInput(DTO):
    name: str = Field(min_length=1, max_length=160)
    root_candidate: str = Field(min_length=1, max_length=4096)


class ProjectInput(DTO):
    name: str = Field(min_length=1, max_length=160)
    roots: list[str] = Field(min_length=1, max_length=32)


class ProjectPatch(ProjectInput):
    expected_revision: int = Field(ge=1)


class SessionInput(DTO):
    workspace_id: str
    agent_spec_id: str = "default"
    title: str | None = Field(default=None, max_length=240)


class SessionPatch(DTO):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    archived: bool | None = None
    expected_revision: int = Field(ge=1)


class TextPart(DTO):
    type: Literal["text"] = "text"
    text: str = Field(max_length=200000)


class FilePart(DTO):
    type: Literal["file_reference"] = "file_reference"
    artifact_ref: ArtifactRef
    label: str | None = None


class ImagePart(DTO):
    type: Literal["image"] = "image"
    artifact_ref: ArtifactRef
    alt: str | None = None


ContentPart = Annotated[TextPart | FilePart | ImagePart, Field(discriminator="type")]


class MessageView(DTO):
    message_id: str
    role: Literal["user", "assistant", "tool"]
    content_parts: list[ContentPart]
    model_attempt_id: str | None = None
    usage_ref: str | None = None


class SelectedSkillRef(DTO):
    skill_id: str
    content_hash: str


class SubmitRunInput(DTO):
    branch_id: str
    expected_branch_revision: int = Field(ge=1)
    agent_spec_revision: int = Field(ge=1)
    content_parts: list[ContentPart] = Field(default_factory=list, max_length=100)
    attachment_refs: list[ArtifactRef] = Field(default_factory=list, max_length=100)
    selected_skill_refs: list[SelectedSkillRef] = Field(default_factory=list, max_length=100)


class SubmitReceipt(DTO):
    run_id: str
    branch_id: str
    branch_revision: int
    status: RunStatus
    revision: int


class RunView(BaseModel):
    model_config = ConfigDict(extra="allow")
    run_id: str
    status: RunStatus
    revision: int
    last_seq: int
    messages: list[MessageView] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    interactions: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    children: list[dict[str, Any]] = Field(default_factory=list)


class CancelInput(DTO):
    reason: str | None = Field(default=None, max_length=2000)


class InteractionResponse(DTO):
    expected_revision: int = Field(ge=1)
    response: dict[str, Any]
    binding_ref: ApprovalBinding | str | None = None


class ReconciliationInput(DTO):
    expected_revision: int = Field(ge=1)
    decision: Literal["confirm_completed", "confirm_not_executed", "accept_unresolved"]
    evidence_refs: list[ArtifactRef] = Field(default_factory=list)
    result_ref: ArtifactRef | None = None
    note: str = Field(default="", max_length=20000)


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    expected_revision: int | None = Field(ge=1)


class ModelTestInput(DTO):
    tests: list[Literal["transport", "messages", "capabilities", "tool_calling", "streaming", "usage"]] = Field(default_factory=lambda: ["transport", "messages", "capabilities"], min_length=1, max_length=6)


class ModelDiscoveryInput(DTO):
    provider_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.-]+$")
    api_key: SecretStr | None = Field(default=None, max_length=32768)
    profile_id: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)


class ModelSetupInput(ModelDiscoveryInput):
    model_id: str = Field(min_length=1, max_length=512)


class ModelSetupSaveInput(ModelSetupInput):
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def edit_revision(self):
        if bool(self.profile_id) != (self.expected_revision is not None):
            raise ValueError("Existing profiles require their expected revision")
        return self


class SkillRefreshInput(DTO):
    source_ids: list[str] = Field(default_factory=list, max_length=100)


class SkillPatch(DTO):
    enabled: bool
    expected_revision: int = Field(ge=1)


class McpDiagnoseInput(DTO):
    levels: list[Literal["transport", "protocol", "catalog", "authentication"]] = Field(default_factory=lambda: ["transport", "protocol", "catalog"], min_length=1, max_length=4)


class BranchInput(DTO):
    source_checkpoint_ref: CheckpointRef
    side_effect_policy: Literal["preserve_external"]
    expected_branch_revision: int = Field(ge=1)


class SteeringInput(DTO):
    text: str = Field(min_length=1, max_length=200000)
    expected_input_revision: int = Field(ge=1)


class MemoryReview(DTO):
    decision: Literal["accept", "reject"]
    expected_revision: int = Field(ge=1)


class MemoryPatch(DTO):
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=200000)
    expires_at: str | None = None
    source_refs: list[str] | None = None


class ContextPinInput(DTO):
    text: str = Field(min_length=1, max_length=20000)
    expected_context_revision: int = Field(ge=0)


class PluginLoadInput(DTO):
    manifest: PluginManifest


ConfigKind = Literal["models", "agents", "mcp", "skill_sources", "policies"]
