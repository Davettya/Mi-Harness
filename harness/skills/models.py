"""Skill domain contracts (implementation guide 08); no executable dependencies."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StateRepository(Protocol):
    """Persistence belongs to the storage module; adapters must commit atomically."""
    def get(self, namespace: str, key: str) -> dict | None: ...
    def put(self, namespace: str, key: str, value: dict) -> None: ...
    def list(self, namespace: str) -> list[dict]: ...
    def delete(self, namespace: str, key: str) -> None: ...
    def compare_and_set(self, namespace: str, key: str, expected_revision: int, value: dict) -> None: ...


class SkillRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill_id: str
    source_id: str
    revision: int = 1
    relative_path: str
    name: str
    description: str = ""
    root_uri: str
    content_hash: str = ""
    enabled: bool = True
    scope: Literal["built_in", "user", "project", "remote"]
    source_revision: str | None = None
    source_url: str | None = None
    license: str | None = None
    compatibility: str | None = None
    trust_state: Literal["trusted", "unreviewed", "blocked"] = "unreviewed"
    validation_diagnostics: list[str] = Field(default_factory=list)
    dependency_metadata: dict[str, Any] = Field(default_factory=dict)


class SkillActivation(BaseModel):
    activation_ref: str
    owner_id: str
    workspace_id: str
    session_id: str
    run_id: str
    agent_id: str
    skill_id: str
    content_hash: str
    activated_by: Literal["user", "model"]
    reason: str
    loaded_resource_refs: list[str] = Field(default_factory=list)
    activated_at: datetime = Field(default_factory=utcnow)
    deactivated_at: datetime | None = None


class ActivatedSkill(BaseModel):
    activation: SkillActivation
    record: SkillRecord
    body: str
    snapshot_ref: str
    resources: list[str]


class ResourceContent(BaseModel):
    content: bytes
    mime_type: str
    source_uri: str
    content_hash: str
    snapshot_ref: str


class RefreshReport(BaseModel):
    refreshed: list[SkillRecord] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    name_conflicts: dict[str, list[str]] = Field(default_factory=dict)
