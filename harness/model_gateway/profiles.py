"""Immutable provider capability snapshots. Unknown is deliberately not zero."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_MODEL_CONTEXT_WINDOW = 300_000
EXTENDED_MODEL_CONTEXT_WINDOW = 1_000_000
CONFIGURABLE_MODEL_CONTEXT_WINDOWS = {
    DEFAULT_MODEL_CONTEXT_WINDOW,
    EXTENDED_MODEL_CONTEXT_WINDOW,
}


class CapabilityState(StrEnum):
    verified = "verified"
    unverified = "unverified"
    unsupported = "unsupported"


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: CapabilityState = CapabilityState.unverified
    evidence_ref: str | None = None

    @model_validator(mode="after")
    def evidence_required(self):
        if self.status == CapabilityState.verified and not self.evidence_ref:
            raise ValueError("Verified capability requires evidence_ref")
        return self


class ModelLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    context_window: int | None = Field(default=DEFAULT_MODEL_CONTEXT_WINDOW, gt=0)
    input_limit: int | None = Field(default=None, gt=0)
    output_limit: int | None = Field(default=None, gt=0)
    reasoning_tokens: Literal["output", "input", "separate", "unknown"] = "unknown"
    source_ref: str | None = None


def configured_model_limits(
    context_window: int = DEFAULT_MODEL_CONTEXT_WINDOW,
    *,
    previous: ModelLimits | None = None,
    output_limit: int | None = 2048,
) -> ModelLimits:
    """Build the application-owned context tier without inventing provider evidence."""
    if context_window not in CONFIGURABLE_MODEL_CONTEXT_WINDOWS:
        raise ValueError("Context window must be the 300K default or explicit 1M tier")
    return ModelLimits(
        context_window=context_window,
        input_limit=None,
        output_limit=previous.output_limit if previous and previous.output_limit else output_limit,
        reasoning_tokens=previous.reasoning_tokens if previous else "unknown",
        source_ref=(
            "user-configured:model-context-window-1m-v1"
            if context_window == EXTENDED_MODEL_CONTEXT_WINDOW
            else "app:default-model-context-window-300k-v1"
        ),
    )


class TokenEstimatorSpec(BaseModel):
    algorithm: str = "utf8-conservative-v1"
    safety_margin: float = Field(default=0.2, ge=0, le=1)
    exact: bool = False


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    profile_id: str
    revision: int = Field(default=1, ge=1)
    provider_id: str
    adapter_id: Literal["openai", "anthropic", "ollama", "demo"]
    adapter_version: str = "1"
    endpoint_ref: str
    credential_ref: str | None = None
    model_id: str
    api_mode: str
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    limits: ModelLimits = Field(default_factory=ModelLimits)
    token_estimator: TokenEstimatorSpec = Field(default_factory=TokenEstimatorSpec)
    verification_ref: str | None = None
    continuation_requirements: dict[str, Any] = Field(default_factory=dict)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)

    @property
    def ref(self) -> str:
        return f"{self.profile_id}@{self.revision}"

    def supports(self, name: str) -> bool:
        return self.capabilities.get(name, Capability()).status == CapabilityState.verified


class TokenEstimate(BaseModel):
    total: int
    categories: dict[str, int]
    algorithm_version: str
    exact: bool = False
    error_margin: float = 0.2


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    source: Literal["provider", "estimated", "unknown"] = "unknown"
    cost: float | None = None


class GatewayError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code, self.retryable = code, retryable


def demo_profile() -> ModelProfile:
    evidence = "fixture:deterministic-demo-v1;not-a-provider-verification"
    return ModelProfile(
        profile_id="demo",
        provider_id="local-demo",
        adapter_id="demo",
        endpoint_ref="demo://local",
        model_id="deterministic-demo-v1",
        api_mode="fixture",
        capabilities={
            n: Capability(status="verified", evidence_ref=evidence)
            for n in ("text", "tool_calling", "parallel_tools", "usage")
        },
        limits=configured_model_limits(),
        verification_ref=evidence,
    )
