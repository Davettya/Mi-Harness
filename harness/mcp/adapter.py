"""MCP tools enter the existing Tool Gateway, ledger, policy and artifact path."""
from __future__ import annotations

from harness.core.contracts import OperationKey, content_hash
from harness.tools.contracts import ToolSpec, ToolResult
from .models import McpInvocation
from .models import McpInputRequired
from harness.tools.contracts import ToolWait


def ledger_invocation_validator(store):
    def validate(invocation):
        ctx = invocation.execution_context
        store.assert_fence(ctx)
        operation = store.operation(invocation.operation_id)
        if (operation["run_id"] != ctx.run_id or operation["state"] != "running"
                or not operation.get("grant_id")
                or invocation.policy_decision_ref != operation["grant_id"]
                or operation["message_id"] != invocation.operation_key.message_id
                or operation["tool_call_id"] != invocation.operation_key.tool_call_id
                or operation["args_hash"] != content_hash(invocation.arguments)):
            raise PermissionError("MCP invocation must match a running, granted Tool Gateway operation")
    return validate


def outcome_to_tool_result(outcome, operation, effect):
    status = {"success": "succeeded", "tool_error": "failed", "protocol_error": "failed",
        "transport_error": "failed", "timeout": "failed", "unknown_outcome": "unknown", "cancelled": "cancelled"}[outcome.kind]
    if outcome.request_sent and effect != "read" and outcome.kind in ("cancelled", "protocol_error"):
        status = "unknown"
    text = "\n".join(block.get("text", "") for block in outcome.content if block.get("type") == "text")
    return ToolResult(operation_id=operation["id"], status=status, summary=text[:2000] or outcome.kind,
        structured_data={"value": outcome.structured_content, "server_id": outcome.server_id,
            "request_sent": outcome.request_sent, "outcome": outcome.kind, "diagnostics": outcome.diagnostics,
            "remote_operation_ref": outcome.remote_operation_ref,
            "cancellation_evidence": outcome.cancellation_evidence},
        content_blocks=outcome.content, error_code=outcome.error_code, retryable=False)


class DurableMcpExecutor:
    def __init__(self, gateway, continuations, catalog, tool, effect):
        self.gateway, self.continuations, self.catalog, self.tool, self.effect = gateway, continuations, catalog, tool, effect

    def invocation(self, ctx, operation, arguments):
        return McpInvocation(operation_key=OperationKey(run_id=ctx.run_id,
                message_id=operation["message_id"], tool_call_id=operation["tool_call_id"]),
            operation_id=operation["id"], execution_context=ctx, server_id=self.catalog.server_id,
            original_name=self.tool.original_name, schema_hash=self.tool.schema_hash, arguments=arguments,
            deadline=ctx.deadline_at, policy_decision_ref=operation["grant_id"])

    def result(self, ctx, operation, outcome):
        if isinstance(outcome, McpInputRequired):
            try:
                return self.continuations.suspend(ctx, operation, outcome, schema_hash=self.tool.schema_hash,
                                                 config_revision=self.catalog.config_revision)
            except Exception:
                return ToolResult(operation_id=operation["id"], status="unknown" if self.effect != "read" else "failed",
                    summary="MCP 人工输入无法安全持久化或包含不支持的请求；没有重发原始操作", error_code="MCP_INPUT_UNAVAILABLE")
        return outcome_to_tool_result(outcome, operation, self.effect)

    async def __call__(self, ctx, operation, arguments):
        profile = await self.gateway._profile(self.catalog.server_id, ctx)
        if "elicitation" not in profile.allowed_capabilities or self.catalog.protocol_version != "2026-07-28":
            return outcome_to_tool_result(await self.gateway.invoke(self.invocation(ctx, operation, arguments)), operation, self.effect)
        return self.result(ctx, operation, await self.gateway.invoke_step(self.invocation(ctx, operation, arguments)))

    async def resume_continuation(self, ctx, operation, arguments):
        profile = await self.gateway._profile(self.catalog.server_id, ctx)
        if "elicitation" not in profile.allowed_capabilities:
            raise PermissionError("MCP elicitation permission revoked")
        resumed = self.continuations.responses(ctx, operation, schema_hash=self.tool.schema_hash,
                                              config_revision=profile.config_revision)
        if isinstance(resumed, ToolWait):
            return resumed
        current, responses, request_state = resumed
        outcome = await self.gateway.invoke_step(self.invocation(ctx, current, arguments),
                                                input_responses=responses, request_state=request_state)
        result = self.result(ctx, current, outcome)
        if not isinstance(result, ToolWait) and result.status != "unknown":
            self.continuations.complete(ctx, current)
        return result


def register_catalog(gateway, tool_registry, catalog, *, trusted_effects: dict[str, str] | None = None, continuations=None):
    """Effect overrides are trusted Host configuration, never server annotations."""
    registered = []
    for tool in catalog.tools:
        effect = (trusted_effects or {}).get(tool.original_name, "external_write")
        spec = ToolSpec(id=tool.safe_alias, version=tool.revision, description=tool.description or tool.original_name,
            origin="mcp:" + catalog.server_id, input_schema=tool.input_schema, output_schema={"type": "object"},
            effect=effect, retry_class="safe" if effect == "read" else "manual_reconcile",
            required_capabilities=["mcp:" + catalog.server_id])

        async def execute(ctx, operation, arguments, *, bound_tool=tool, bound_effect=effect):
            invocation = McpInvocation(operation_key=OperationKey(run_id=ctx.run_id,
                    message_id=operation["message_id"], tool_call_id=operation["tool_call_id"]),
                operation_id=operation["id"], execution_context=ctx, server_id=catalog.server_id,
                original_name=bound_tool.original_name, schema_hash=bound_tool.schema_hash, arguments=arguments,
                deadline=ctx.deadline_at, policy_decision_ref=operation["grant_id"])
            outcome = await gateway.invoke(invocation)
            return outcome_to_tool_result(outcome, operation, bound_effect)
        if continuations is not None:
            execute = DurableMcpExecutor(gateway, continuations, catalog, tool, effect)
        tool_registry.register(spec, execute)
        registered.append(spec)
    return registered
