"""Durable, single-exchange modern MCP elicitation without replaying tools/call."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jsonschema
from mcp.types import ElicitResult
from harness.core import HarnessError, content_hash
from harness.tools.contracts import ToolWait
from .models import McpInputRequired


class McpContinuationService:
    def __init__(self, store, vault, *, max_rounds=8, interaction_ttl=900):
        self.store, self.vault = store, vault
        self.max_rounds, self.interaction_ttl = max_rounds, interaction_ttl

    def suspend(self, ctx, operation, pending: McpInputRequired, *, schema_hash, config_revision):
        old = (operation.get("metadata") or {}).get("protocol_continuation") or {}
        round_number = old.get("round", 0) + 1
        if round_number > self.max_rounds or not pending.input_requests or len(pending.input_requests) > 8:
            raise HarnessError("MCP_INPUT_LIMIT", "MCP 人工输入轮次或表单数量超过限制", 422)
        schemas, messages = {}, []
        for key, request in pending.input_requests.items():
            params = request.get("params") or {}
            if request.get("method") != "elicitation/create" or params.get("mode", "form") != "form":
                raise HarnessError("MCP_INPUT_UNSUPPORTED", "MCP sampling、roots、URL-mode 或未知输入未启用", 422)
            schema = params.get("requestedSchema", params.get("requested_schema"))
            if not isinstance(schema, dict) or len(json.dumps(schema)) > 65536:
                raise HarnessError("MCP_FORM_SCHEMA", "MCP 表单 schema 无效或过大", 422)
            jsonschema.Draft202012Validator.check_schema(schema)
            schemas[key] = {"oneOf": [
                {"type": "object", "properties": {"action": {"const": "accept"}, "content": schema},
                 "required": ["action", "content"], "additionalProperties": False},
                {"type": "object", "properties": {"action": {"enum": ["decline", "cancel"]}},
                 "required": ["action"], "additionalProperties": False}]}
            messages.append(str(params.get("message", "MCP 请求输入"))[:4000])
        response_schema = {"type": "object", "properties": {"responses": {"type": "object", "properties": schemas,
            "required": list(schemas), "additionalProperties": False}}, "required": ["responses"], "additionalProperties": False}
        request_hash = content_hash(pending.input_requests)
        secret = {"request_state": pending.request_state, "input_requests": pending.input_requests}
        reference = self.vault.put("mcp-continuation:" + operation["id"], json.dumps(secret))
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=self.interaction_ttl)).isoformat()
        try:
            with self.store.transaction():
                current = self.store.operation(operation["id"])
                # Caller must own the exact operation revision obtained before this exchange.
                if current["revision"] != operation["revision"]:
                    raise HarnessError("REVISION_CONFLICT", "MCP 操作已被另一执行者更新")
                interaction = self.store.prepare_interaction(ctx,
                    f"mcp:{operation['id']}:{round_number}:{request_hash}", "mcp_elicitation",
                    {"prompt": "\n\n".join(messages), "response_schema": response_schema,
                     "operation_id": operation["id"], "server_id": pending.server_id,
                     "mcp_requests": pending.input_requests, "protocol": pending.protocol_version,
                     "mcp_binding": {"operation_id": operation["id"], "request_hash": request_hash,
                                     "schema_hash": schema_hash, "round": round_number}}, expiry)
                continuation = {"kind": "modern_mrtr", "status": "awaiting_input", "request_state_ref": reference,
                    "request_hash": request_hash, "schema_hash": schema_hash, "interaction_id": interaction["id"],
                    "round": round_number, "server_id": pending.server_id, "config_revision": config_revision}
                self.store.update_operation(operation["id"], current["revision"],
                    {"metadata": {**current["metadata"], "protocol_continuation": continuation}}, ctx)
        except Exception:
            self.vault.delete(reference, "mcp-continuation:" + operation["id"])
            raise
        if old.get("request_state_ref"):
            self.vault.delete(old["request_state_ref"], "mcp-continuation:" + operation["id"])
        return ToolWait(kind="user", interaction_id=interaction["id"], operation_id=operation["id"])

    def responses(self, ctx, operation, *, schema_hash, config_revision):
        continuation = (operation.get("metadata") or {}).get("protocol_continuation") or {}
        if (continuation.get("kind") != "modern_mrtr" or continuation.get("status") != "awaiting_input"
                or continuation.get("schema_hash") != schema_hash or continuation.get("config_revision") != config_revision):
            raise HarnessError("MCP_CONTINUATION_STALE", "MCP 续接状态、目录或连接配置已变化", 409)
        interaction = self.store.interaction(continuation["interaction_id"])
        if interaction["run_id"] != ctx.run_id or interaction.get("operation_id") != operation["id"]:
            raise HarnessError("MCP_CONTINUATION_SCOPE", "MCP 表单不属于当前操作", 403)
        if interaction["status"] in {"pending_binding", "open"}:
            return ToolWait(kind="user", interaction_id=interaction["id"], operation_id=operation["id"])
        secret = json.loads(self.vault.get(continuation["request_state_ref"], "mcp-continuation:" + operation["id"]))
        if content_hash(secret["input_requests"]) != continuation["request_hash"]:
            raise HarnessError("MCP_CONTINUATION_INTEGRITY", "MCP 续接请求摘要不匹配", 409)
        resolution = interaction.get("resolution") or {}
        if resolution.get("outcome") == "answered":
            answer = resolution.get("response", resolution.get("response_ref", {}))
            jsonschema.validate(answer, interaction["response_schema"])
            responses = {key: ElicitResult.model_validate(value) for key, value in answer["responses"].items()}
        else:
            # Expiry/rejection communicates protocol cancel, and grants no new authority.
            responses = {key: ElicitResult(action="cancel") for key in secret["input_requests"]}
        with self.store.transaction():
            current = self.store.operation(operation["id"])
            if current["revision"] != operation["revision"]:
                raise HarnessError("REVISION_CONFLICT", "MCP 续接答复已被消费")
            updated = self.store.update_operation(operation["id"], operation["revision"],
                {"metadata": {**operation["metadata"], "protocol_continuation": {**continuation, "status": "sending",
                    "response_hash": content_hash({key: value.model_dump(mode="json") for key, value in responses.items()})}}}, ctx)
        return updated, responses, secret["request_state"]

    def complete(self, ctx, operation):
        continuation = (operation.get("metadata") or {}).get("protocol_continuation") or {}
        if continuation.get("request_state_ref"):
            self.vault.delete(continuation["request_state_ref"], "mcp-continuation:" + operation["id"])
