from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit
from pydantic import Field, model_validator
from harness.core.contracts import DTO, ExecutionContext, OperationKey, utc_now


class ConnectionProfile(DTO):
    server_id: str
    config_revision: int = 1
    display_name: str
    transport: Literal["stdio", "streamable_http", "legacy_sse"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    cwd_ref: str | None = None
    endpoint: str | None = None
    environment_refs: dict[str, str] = Field(default_factory=dict)
    credential_ref: str | None = None
    oauth_ref: str | None = None
    allowed_capabilities: list[str] = Field(default_factory=lambda: ["tools"])
    protocol_policy: Literal["auto", "modern_only", "legacy_only"] = "auto"
    connection_scope: Literal["call", "run"] = "call"
    connect_timeout: float = Field(default=10, gt=0, le=120)
    request_timeout: float = Field(default=60, gt=0, le=3600)
    max_total_duration: float = Field(default=180, gt=0, le=86400)
    catalog_ttl: float = Field(default=30, ge=0, le=3600)

    @model_validator(mode="after")
    def valid_transport(self):
        if self.credential_ref is not None and not self.credential_ref.startswith("credential:"):
            raise ValueError("MCP credentials must use a credential: reference")
        if self.oauth_ref is not None and self.oauth_ref != "oauth:"+self.server_id:
            raise ValueError("OAuth reference must be bound to this MCP server")
        for name, reference in self.environment_refs.items():
            if not name or "=" in name or "\x00" in name or not reference.startswith("credential:"):
                raise ValueError("MCP environment values must use credential: references")
        if self.transport == "stdio":
            if not self.command or self.endpoint:
                raise ValueError("stdio requires command and cannot have endpoint")
        else:
            parsed = urlsplit(self.endpoint or "")
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("MCP HTTP endpoint must be absolute with no userinfo or fragment")
            if self.command or self.args or self.environment_refs or self.cwd_ref:
                raise ValueError("HTTP profile cannot contain process configuration")
            if self.transport == "legacy_sse" and self.protocol_policy != "legacy_only":
                raise ValueError("legacy SSE requires explicit legacy_only protocol policy")
        if set(self.allowed_capabilities) - {"tools", "resources", "prompts", "elicitation"}:
            raise ValueError("unimplemented MCP capability cannot be enabled")
        return self


class CatalogTool(DTO):
    original_name: str
    safe_alias: str
    schema_hash: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)
    description: str = ""
    revision: str


class CatalogSnapshot(DTO):
    server_id: str
    principal_fingerprint: str
    config_revision: int
    protocol_version: str
    discovered_at: str = Field(default_factory=utc_now)
    expires_at: str | None = None
    complete: bool
    diagnostics: list[str] = Field(default_factory=list)
    tools: list[CatalogTool] = Field(default_factory=list)


class McpInvocation(DTO):
    operation_key: OperationKey
    operation_id: str
    execution_context: ExecutionContext
    server_id: str
    original_name: str
    schema_hash: str
    arguments: dict[str, Any]
    deadline: str | None = None
    policy_decision_ref: str
    remote_operation_ref: str | None = None


class McpOutcome(DTO):
    kind: Literal["success", "tool_error", "protocol_error", "transport_error", "cancelled", "timeout", "unknown_outcome"]
    server_id: str
    operation_id: str
    request_sent: bool
    content: list[dict[str, Any]] = Field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False
    error_code: str | None = None
    remote_operation_ref: str | None = None
    cancellation_evidence: str | None = None
    diagnostics: list[str] = Field(default_factory=list)


class McpInputRequired(DTO):
    """Internal-only handoff. Opaque request state must go straight to the vault."""
    server_id: str
    operation_id: str
    protocol_version: str
    input_requests: dict[str, Any]
    request_state: str | None = None


class CancelOutcome(DTO):
    accepted: bool
    transport_closed: bool = False
    remote_confirmed: bool | None = None
    error: str | None = None


class DiagnosticReport(DTO):
    server_id: str
    diagnostic_id: str
    protocol_version: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)
    layers: dict[str, dict[str, Any]]
    diagnostics: list[str] = Field(default_factory=list)


class SourcedContent(DTO):
    server_id: str
    reference: str
    protocol_version: str
    content: dict[str, Any]
    trusted_instructions: Literal[False] = False
