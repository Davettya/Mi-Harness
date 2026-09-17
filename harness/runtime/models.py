from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.core import ApprovalBinding, ArtifactRef, CheckpointRef, new_id


class AgentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = "default"
    revision: int = Field(default=1, ge=1)
    runtime: Literal["langchain"] = "langchain"
    system_prompt_ref: str = "builtin:default"
    model_policy: dict[str, Any] = Field(default_factory=lambda: {"profile_ref": "demo@1"})
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    policy: str = "default"
    context_policy: str = "default"
    budget: dict[str, Any] = Field(default_factory=lambda: {"max_model_calls": 100})

    def validate_references(self, resolver):
        for kind, refs in (
            ("prompts", [self.system_prompt_ref]),
            ("tools", self.tools),
            ("skills", self.skills),
            ("mcp", self.mcp_servers),
            ("policy", [self.policy]),
            ("context_policy", [self.context_policy]),
        ):
            for ref in refs:
                if not resolver(kind, ref):
                    raise ValueError(f"Unresolved {kind} reference: {ref}")


class InteractionRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interaction_id: str = Field(default_factory=new_id)
    run_id: str
    input_revision: int
    interrupt_id: str | None = None
    checkpoint_ref: CheckpointRef | None = None
    kind: Literal["approval", "user_input", "mcp_elicitation"]
    prompt: str
    response_schema: dict
    payload_ref: ArtifactRef | None = None
    revision: int = 1
    expires_at: str | None = None
    binding_ref: ApprovalBinding | str | None = None
    status: Literal["pending_binding", "open", "resolved", "expired", "cancelled"] = "pending_binding"

    @model_validator(mode="after")
    def validate_binding(self):
        if self.kind == "approval" and self.binding_ref is None:
            raise ValueError("Approval interaction requires a concrete binding")
        if self.status == "open" and (not self.interrupt_id or not self.checkpoint_ref):
            raise ValueError("An open interaction requires a durable interrupt and checkpoint")
        return self


class InterruptResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    interrupt_id: str
    outcome: Literal["answered", "expired", "cancelled"]
    response_ref: str | None = None
    reason: str
