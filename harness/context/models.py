from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.core import ArtifactRef, new_id, utc_now


class ContextPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    revision: int = 3
    output_reserve: int = Field(default=8192, gt=0)
    safety_tokens: int = Field(default=256, ge=0)
    soft_threshold: float = Field(default=0.75, gt=0, lt=1)
    compaction_target: float = Field(default=0.55, gt=0, lt=1)
    offload_bytes: int = Field(default=16384, ge=256)
    recent_turns: int = Field(default=3, ge=1)


class SourceRevision(BaseModel):
    model_config = ConfigDict(frozen=True)
    input_revision: int
    context_revision: int
    pin_revision: int = 0


class ContextView(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    input_view_id: str = Field(default_factory=new_id)
    source_revision: SourceRevision
    message_refs: list[str]
    material_refs: list[ArtifactRef] = Field(default_factory=list)
    selected_tool_refs: list[str] = Field(default_factory=list)
    tools_schema_hash: str
    skill_snapshot_refs: list[str] = Field(default_factory=list)
    summary_refs: list[str] = Field(default_factory=list)
    pin_refs: list[str] = Field(default_factory=list)
    model_profile_ref: str
    budget_breakdown: dict[str, Any]
    normalized_request_hash: str
    input_snapshot_ref: str | None = None


class CompactionSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    summary_id: str = Field(default_factory=new_id)
    revision: int = 1
    covered_message_refs: list[str]
    parent_summary_refs: list[str] = Field(default_factory=list)
    goal: str
    constraints: list[str]
    verified_facts: list[str] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    failed_approaches: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    source_manifest_hash: str
    content_hash: str
    model_profile_ref: str
    created_at: str = Field(default_factory=utc_now)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(default_factory=new_id)
    revision: int = 1
    owner: str
    workspace: str
    scope: Literal["workspace", "user"] = "workspace"
    kind: Literal["fact", "preference", "procedure", "pinned"] = "fact"
    content: str = Field(min_length=1)
    source_refs: list[str | dict[str, Any]] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now)
    expires_at: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    review_state: Literal["proposed", "accepted", "rejected", "deleted"] = "proposed"

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("Memory expiry requires an explicit timezone")
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
