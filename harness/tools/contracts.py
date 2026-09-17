from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from harness.core import ArtifactRef


class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    version: str = "1"
    description: str
    origin: str = "builtin"
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    effect: Literal["read", "workspace_write", "external_write", "process"] = "read"
    retry_class: Literal["safe", "idempotency_key_required", "manual_reconcile"] = "safe"
    required_capabilities: list[str] = Field(default_factory=list)
    timeout: float = 60
    output_limit: int = 1024 * 1024
    concurrency_key: str = "operation"


class ToolResult(BaseModel):
    operation_id: str
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    summary: str
    structured_data: dict[str, Any] = Field(default_factory=dict)
    content_blocks: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    truncated: bool = False
    exit_code: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error_code: str | None = None
    retryable: bool = False


class ToolWait(BaseModel):
    kind: Literal["user", "children", "review"]
    interaction_id: str | None = None
    wait_id: str | None = None
    operation_id: str | None = None
