from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def new_id() -> str:
    return str(uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RunStatus(str, Enum):
    queued = "queued"
    running = "running"
    waiting_user = "waiting_user"
    waiting_children = "waiting_children"
    recovering = "recovering"
    needs_review = "needs_review"
    cancelling = "cancelling"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


TERMINAL_STATUSES = frozenset({RunStatus.completed, RunStatus.failed, RunStatus.cancelled})


class ExecutionContext(DTO):
    owner_id: str
    workspace_id: str
    session_id: str
    branch_id: str
    run_id: str
    root_run_id: str
    parent_run_id: str | None = None
    worker_attempt_id: str
    fencing_token: int
    graph_thread_key: str
    input_revision: int
    config_snapshot_id: str
    deadline_at: str | None = None
    trace_id: str


class DiagnosticContext(DTO):
    diagnostic_id: str
    owner_id: str
    workspace_id: str | None = None
    config_snapshot_id: str
    budget_account_id: str
    deadline_at: str
    trace_id: str


AccessContext = ExecutionContext | DiagnosticContext


class ConfigSnapshot(DTO):
    config_snapshot_id: str = Field(default_factory=new_id)
    schema_version: Literal[1] = 1
    agent_spec: dict[str, Any]
    model_profile: dict[str, Any]
    context_policy: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    plugins: list[dict[str, Any]] = Field(default_factory=list)
    mcp: list[dict[str, Any]] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    egress: dict[str, Any] = Field(default_factory=dict)


class OperationKey(DTO):
    run_id: str
    message_id: str
    tool_call_id: str


class ApprovalBinding(DTO):
    operation_id: str
    tool_revision: str
    args_hash: str
    resource_fingerprint: str
    policy_revision: int
    expires_at: str


class ArtifactRef(DTO):
    artifact_id: str
    content_hash: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    owner_id: str
    workspace_id: str
    provenance_ref: str


class CheckpointRef(DTO):
    graph_thread_key: str
    checkpoint_ns: str = ""
    checkpoint_id: str
    runtime_revision: str = "langchain-v1"


class InterruptRef(DTO):
    interrupt_id: str
    checkpoint_ref: CheckpointRef
    kind: Literal["user", "children", "review"]
    interaction_id: str | None = None
    wait_id: str | None = None
    operation_id: str | None = None


class ErrorEnvelope(DTO):
    code: str
    message: str
    retryable: bool = False
    correlation_id: str = Field(default_factory=new_id)
    details_ref: ArtifactRef | None = None


class RuntimeOutcome(DTO):
    kind: Literal["final", "waiting", "error", "cancel_ack"]
    run_id: str
    input_revision: int
    checkpoint_ref: CheckpointRef | None = None
    result_ref: ArtifactRef | None = None
    interrupt_refs: list[InterruptRef] = Field(default_factory=list)
    error: ErrorEnvelope | None = None


class EventEnvelope(DTO):
    schema_version: Literal[1] = 1
    event_id: str = Field(default_factory=new_id)
    run_id: str
    type: str
    timestamp: str = Field(default_factory=utc_now)
    durability: Literal["durable", "ephemeral"] = "durable"
    seq: int | None = None
    data: dict[str, Any]

    @model_validator(mode="after")
    def validate_sequence(self):
        if self.durability == "ephemeral" and self.seq is not None:
            raise ValueError("Ephemeral events cannot advance a durable cursor")
        if self.durability == "durable" and (self.seq is None or self.seq < 1):
            raise ValueError("Durable events require a positive sequence")
        return self


class HarnessError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable

    def envelope(self) -> ErrorEnvelope:
        return ErrorEnvelope(code=self.code, message=self.message, retryable=self.retryable)
