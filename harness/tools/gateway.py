from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
from weakref import WeakValueDictionary
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import portalocker

from harness.core import (
    ApprovalBinding,
    ExecutionContext,
    HarnessError,
    OperationKey,
    canonical_json,
    content_hash,
    new_id,
    utc_now,
)
from .contracts import ToolResult, ToolWait


def after_seconds(seconds: float) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ToolGateway:
    def __init__(self, store, artifacts, policy, registry, data_dir: Path, *, budget=None, fault_hook=None):
        self.store, self.artifacts, self.policy, self.registry, self.data_dir = (
            store,
            artifacts,
            policy,
            registry,
            Path(data_dir),
        )
        self.budget, self.fault_hook = budget, fault_hook
        self._locks = WeakValueDictionary()
        (self.data_dir / "locks").mkdir(parents=True, exist_ok=True)

    def _version(self, ctx, name):
        snapshot = self.store.get("snapshots", ctx.config_snapshot_id) or {}
        permitted = {x["id"]: x["version"] for x in snapshot.get("tools", [])}
        if name not in permitted:
            raise HarnessError("TOOL_NOT_IN_SNAPSHOT", "该工具不在本次任务的固定能力清单内", 403)
        return permitted[name]

    def _resources(self, ctx, spec, args):
        if spec.origin.startswith("mcp:"):
            return content_hash(dict(tool=spec.id, args=args))
        paths = [args["path"]] if "path" in args else [args["cwd"]] if "cwd" in args else []
        if paths:
            return self.policy.fingerprint(ctx, paths, write=spec.effect == "workspace_write")
        return content_hash(dict(tool=spec.id, args=args))

    async def execute(
        self, ctx: ExecutionContext, key: OperationKey, name: str, args: dict
    ) -> ToolResult | ToolWait:
        if not isinstance(ctx, ExecutionContext) or key.run_id != ctx.run_id:
            raise HarnessError("TOOL_IDENTITY", "无效工具执行身份", 403)
        self.store.assert_fence(ctx)
        spec, executor = self.registry.get(name, self._version(ctx, name))
        from harness.runtime.modes import enforce
        enforce(self.store.get("snapshots", ctx.config_snapshot_id) or {}, spec)
        try:
            jsonschema.validate(args, spec.input_schema)
            args_digest = content_hash(args)
        except (jsonschema.ValidationError, ValueError) as exc:
            return ToolResult(
                operation_id="",
                status="failed",
                summary="工具参数不符合 schema",
                error_code="TOOL_ARGUMENTS",
                retryable=False,
            )
        lock_key = content_hash(key)
        async with self._locks.setdefault(lock_key, asyncio.Lock()):
            existing = self.store.operation_by_key(ctx.run_id, key.message_id, key.tool_call_id)
            if existing:
                if (existing["args_hash"], existing["tool_id"], existing["tool_revision"]) != (
                    args_digest,
                    name,
                    spec.version,
                ):
                    raise HarnessError("OPERATION_CONFLICT", "同一操作不得更换参数或实现版本")
                if existing["result"] and existing["state"] in {"succeeded", "failed", "cancelled"}:
                    return ToolResult.model_validate(existing["result"])
                if existing["state"] in {"running", "unknown"}:
                    continuation = (existing.get("metadata") or {}).get("protocol_continuation") or {}
                    if (
                        existing["state"] == "running"
                        and continuation.get("kind") == "modern_mrtr"
                        and continuation.get("status") == "awaiting_input"
                        and hasattr(executor, "resume_continuation")
                    ):
                        return await self._resume_protocol(ctx, existing, spec, executor, args)
                    return ToolWait(kind="review", operation_id=existing["id"])
            fingerprint = self._resources(ctx, spec, args)
            operation = self.store.prepare_operation(
                ctx,
                dict(
                    message_id=key.message_id,
                    tool_call_id=key.tool_call_id,
                    tool_id=name,
                    tool_revision=spec.version,
                    args_hash=args_digest,
                    args=args,
                    resource_fingerprint=fingerprint,
                    input_summary=self.input_summary(name, args),
                ),
            )
            decision = self.policy.decide(ctx, spec, args)
            if decision["decision"] == "deny":
                return self._finish(
                    ctx,
                    operation,
                    ToolResult(
                        operation_id=operation["id"],
                        status="failed",
                        summary=decision["reason"],
                        error_code="POLICY_DENIED",
                    ),
                )
            if fingerprint != operation["resource_fingerprint"]:
                return self._finish(
                    ctx,
                    operation,
                    ToolResult(
                        operation_id=operation["id"],
                        status="failed",
                        summary="审批后目标资源已变化，请重新读取并发起新操作",
                        error_code="RESOURCE_CHANGED",
                    ),
                )
            interaction = next(
                (
                    i
                    for i in self.store.interactions(ctx.run_id)
                    if i["request_key"] == "approval:" + operation["id"]
                ),
                None,
            )
            binding = ApprovalBinding(
                operation_id=operation["id"],
                tool_revision=spec.version,
                args_hash=args_digest,
                resource_fingerprint=fingerprint,
                policy_revision=decision["policy_revision"],
                expires_at=after_seconds(900),
            )
            if decision["decision"] == "approval":
                if interaction is None:
                    payload = dict(
                        prompt=f"允许执行 {name}？",
                        response_schema={
                            "type": "object",
                            "properties": {"decision": {"enum": ["approve_once", "approve_scoped", "deny"]}},
                            "required": ["decision"],
                            "additionalProperties": False,
                        },
                        binding_ref=binding.model_dump(mode="json"),
                        operation_preview=dict(
                            tool=name,
                            args=args,
                            effect=spec.effect,
                            scope="此操作的参数和目标",
                            diff=self._preview(ctx, name, args),
                        ),
                    )
                    interaction = self.store.prepare_interaction(
                        ctx, "approval:" + operation["id"], "approval", payload, binding.expires_at
                    )
                binding = ApprovalBinding.model_validate(interaction["binding_ref"])
                if interaction["status"] in {"pending_binding", "open"}:
                    return ToolWait(
                        kind="user", interaction_id=interaction["id"], operation_id=operation["id"]
                    )
                resolution = interaction.get("resolution") or {}
                answer = resolution.get("response", resolution.get("response_ref", {})) or {}
                if resolution.get("outcome") != "answered" or answer.get("decision") not in {
                    "approve_once",
                    "approve_scoped",
                }:
                    return self._finish(
                        ctx,
                        operation,
                        ToolResult(
                            operation_id=operation["id"],
                            status="failed",
                            summary="操作未获批准或交互已过期",
                            error_code="APPROVAL_DENIED",
                        ),
                    )
            if name in {"request_input", "wait_children"}:
                self.store.reserve(ctx.root_run_id, "tool:" + operation["id"], {"tool_calls": 1})
                special = await executor(ctx, operation, args)
                if isinstance(special, ToolWait):
                    return special
                result = self._finish(
                    ctx, operation, ToolResult(operation_id=operation["id"], status="succeeded", **special)
                )
                self.store.settle("tool:" + operation["id"], {"tool_calls": 1})
                return result
            resource_key = (
                os.path.normcase(
                    str(self.policy.path(ctx, args["path"], write=spec.effect == "workspace_write"))
                )
                if "path" in args and not spec.origin.startswith("mcp:")
                else operation["id"]
            )
            async with self._locks.setdefault("resource:" + resource_key, asyncio.Lock()):
                # Single worker, and an OS lock also guards restart overlap.
                resource_lock = portalocker.Lock(
                    str(
                        self.data_dir
                        / "locks"
                        / (hashlib.sha256(resource_key.encode()).hexdigest() + ".lock")
                    ),
                    timeout=0,
                )
                try:
                    resource_lock.acquire()
                except portalocker.exceptions.LockException as exc:
                    raise HarnessError("RESOURCE_BUSY", "目标资源正在被另一操作占用") from exc
                try:
                    current = self.policy.decide(ctx, spec, args)
                    if (
                        current["decision"] == "deny"
                        or current["policy_revision"] != binding.policy_revision
                        or self._resources(ctx, spec, args) != fingerprint
                    ):
                        return self._finish(
                            ctx,
                            operation,
                            ToolResult(
                                operation_id=operation["id"],
                                status="failed",
                                summary="执行前权限或资源复验失败",
                                error_code="BINDING_CHANGED",
                            ),
                        )
                    grant_id = new_id()
                    with self.store.transaction():
                        self.store.add_grant(
                            dict(
                                id=grant_id,
                                operation_id=operation["id"],
                                owner_id=ctx.owner_id,
                                run_id=ctx.run_id,
                                binding=binding.model_dump(mode="json"),
                            )
                        )
                        operation = self.store.update_operation(
                            operation["id"], operation["revision"], dict(state="authorized"), ctx
                        )
                        operation = self.store.consume_grant(
                            ctx, grant_id, binding.model_dump(mode="json"), current["policy_revision"]
                        )
                        self.store.reserve(ctx.root_run_id, "tool:" + operation["id"], {"tool_calls": 1})
                        self.store.event(
                            ctx.run_id,
                            "tool.started",
                            dict(
                                operation_id=operation["id"],
                                tool_id=name,
                                tool_revision=spec.version,
                                input_summary=self.input_summary(name, args),
                                artifact_refs=[],
                            ),
                        )
                    if self.fault_hook:
                        self.fault_hook("before_tool", operation)
                    try:
                        value = executor(ctx, operation, args)
                        if inspect.isawaitable(value):
                            value = await asyncio.wait_for(value, spec.timeout + 10)
                        if isinstance(value, ToolWait):
                            if self._valid_protocol_wait(operation["id"], value):
                                return value
                            raise HarnessError("INVALID_WAIT", "执行中的操作不能转为未登记等待")
                        if isinstance(value, ToolResult):
                            result = value
                        else:
                            value = dict(value)
                            result = ToolResult(
                                operation_id=operation["id"], status=value.pop("status", "succeeded"), **value
                            )
                        jsonschema.validate(result.structured_data, spec.output_schema)
                    except HarnessError as exc:
                        result = ToolResult(
                            operation_id=operation["id"],
                            status="unknown" if exc.code == "PROCESS_EXIT_UNKNOWN" else "failed",
                            summary=exc.message,
                            error_code=exc.code,
                        )
                    except asyncio.CancelledError:
                        result = ToolResult(
                            operation_id=operation["id"],
                            status="unknown" if spec.effect in {"external_write", "process"} else "cancelled",
                            summary="执行已中断；已发生的副作用不会自动撤销",
                            error_code="EXECUTION_INTERRUPTED",
                        )
                    except Exception:
                        result = ToolResult(
                            operation_id=operation["id"],
                            status="unknown" if spec.effect != "read" else "failed",
                            summary="工具执行异常，详细状态需核对",
                            error_code="TOOL_EXECUTION_ERROR",
                        )
                    if self.fault_hook:
                        self.fault_hook("after_tool_before_ledger", operation)
                    result = self._finish(ctx, operation, result)
                    self.store.settle(
                        "tool:" + operation["id"], None if result.status == "unknown" else {"tool_calls": 1}
                    )
                    if result.status == "unknown":
                        return ToolWait(kind="review", operation_id=operation["id"])
                    return result
                finally:
                    resource_lock.release()

    def _valid_protocol_wait(self, operation_id, wait):
        operation = self.store.operation(operation_id)
        continuation = (operation.get("metadata") or {}).get("protocol_continuation") or {}
        return (
            operation["state"] == "running"
            and continuation.get("kind") == "modern_mrtr"
            and continuation.get("status") == "awaiting_input"
            and continuation.get("request_state_ref")
            and continuation.get("interaction_id") == wait.interaction_id
            and wait.operation_id == operation_id
        )

    async def _resume_protocol(self, ctx, operation, spec, executor, args):
        """Resume one persisted modern MCP continuation under the original grant."""
        try:
            decision = self.policy.decide(ctx, spec, args)
            if (
                decision["decision"] == "deny"
                or self._resources(ctx, spec, args) != operation["resource_fingerprint"]
            ):
                raise HarnessError("CONTINUATION_POLICY", "MCP 等待期间权限或资源已变化，需核对", 403)
            self.store.verify_consumed_grant(ctx, operation["id"], decision["policy_revision"])
            result = await asyncio.wait_for(
                executor.resume_continuation(ctx, operation, args), spec.timeout + 10
            )
            if isinstance(result, ToolWait):
                if self._valid_protocol_wait(operation["id"], result):
                    return result
                raise HarnessError("INVALID_WAIT", "MCP 续接等待未持久登记")
            jsonschema.validate(result.structured_data, spec.output_schema)
        except asyncio.CancelledError:
            result = ToolResult(
                operation_id=operation["id"],
                status="unknown",
                summary="MCP 续接已中断，副作用需要核对",
                error_code="CONTINUATION_INTERRUPTED",
            )
        except Exception:
            result = ToolResult(
                operation_id=operation["id"],
                status="unknown",
                summary="MCP 续接无法确认，原始操作未重发",
                error_code="CONTINUATION_UNCERTAIN",
            )
        result = self._finish(ctx, operation, result)
        self.store.settle(
            "tool:" + operation["id"], None if result.status == "unknown" else {"tool_calls": 1}
        )
        return ToolWait(kind="review", operation_id=operation["id"]) if result.status == "unknown" else result

    def _finish(self, ctx, operation, result: ToolResult) -> ToolResult:
        result = result.model_copy(update={"completed_at": utc_now(), "started_at": operation["created_at"]})
        with self.store.transaction():
            current = self.store.operation(operation["id"])
            self.store.update_operation(
                operation["id"],
                current["revision"],
                dict(state=result.status, result=result.model_dump(mode="json")),
                ctx,
            )
            self.store.event(
                ctx.run_id,
                "tool.failed" if result.status == "failed" else "tool.completed",
                dict(
                    operation_id=operation["id"],
                    tool_id=operation["tool_id"],
                    tool_revision=operation["tool_revision"],
                    input_summary=self.input_summary(operation["tool_id"], operation["args"]),
                    result_status=result.status,
                    result_summary=result.summary[:2000],
                    artifact_refs=[r.model_dump(mode="json") for r in result.artifact_refs],
                ),
            )
        return result

    @staticmethod
    def input_summary(name: str, args: dict) -> str:
        return (
            name + ": " + str(args.get("path", args.get("url", args.get("prompt", args.get("argv", "")))))
        )[:300]

    def _preview(self, ctx, name, args):
        if name == "write_file":
            import difflib

            target = self.policy.path(ctx, args["path"], write=True)
            old = (
                target.read_text(encoding="utf-8")
                if target.exists() and target.stat().st_size < 1024 * 1024
                else ""
            )
            return "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    args["content"].splitlines(keepends=True),
                    fromfile=args["path"],
                    tofile=args["path"],
                )
            )[:24000]
        return None

    def reconcile(self, operation_id: str, actor: str, request: dict) -> dict:
        operation = self.store.operation(operation_id)
        self.store.run(operation["run_id"], actor)
        if operation["revision"] != request["expected_revision"] or operation["state"] != "unknown":
            raise HarnessError("REVISION_CONFLICT", "操作已更新或无需核对")
        decision = request["decision"]
        note = request.get("note", "").strip()
        if not note:
            raise HarnessError("EVIDENCE_REQUIRED", "核对必须记录依据", 422)
        for ref in request.get("evidence_refs", []):
            self.artifacts.path(ref["artifact_id"], actor)
        permitted = "none"
        patch = {}
        if decision == "confirm_completed":
            ref = request.get("result_ref")
            if not ref or not request.get("evidence_refs"):
                raise HarnessError("EVIDENCE_REQUIRED", "确认完成须提供可读取结果和关联证据", 422)
            self.artifacts.path(ref["artifact_id"], actor)
            # Arbitrary command/external outcomes remain a human-attested result, never inferred.
            result = ToolResult(
                operation_id=operation_id,
                status="succeeded",
                summary=note,
                structured_data={"reconciled_by": actor, "evidence_refs": request["evidence_refs"]},
                artifact_refs=[ref],
                completed_at=utc_now(),
            )
            patch = dict(state="succeeded", result=result.model_dump(mode="json"))
            permitted = "resume"
        elif decision == "confirm_not_executed":
            metadata = operation["metadata"] or {}
            if not metadata.get("proven_not_started"):
                raise HarnessError(
                    "UNVERIFIABLE_NON_EXECUTION",
                    "当前执行器没有可验证的未执行证据，不能仅凭缺少结果重新执行",
                    422,
                )
            patch = dict(state="prepared", result=None, grant_id=None)
            permitted = "resume"
        elif decision == "accept_unresolved":
            patch = dict(
                metadata={**(operation["metadata"] or {}), "unresolved_accepted": True, "accepted_by": actor}
            )
            permitted = "end_without_success"
        else:
            raise HarnessError("INVALID_DECISION", "核对决定无效", 422)
        with self.store.transaction():
            record = self.store.add_reconciliation(
                operation_id, operation["revision"], actor, decision, request
            )
            updated = self.store.update_operation(operation_id, operation["revision"], patch)
            self.store.revoke_grants(operation["run_id"])
        return dict(
            reconciliation_id=record,
            operation_id=operation_id,
            allowed_action=permitted,
            revision=updated["revision"],
        )
