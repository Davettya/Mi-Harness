from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import jsonschema
import psutil
from pydantic import BaseModel, Field

from harness.core import (
    ExecutionContext,
    HarnessError,
    OperationKey,
    RuntimeOutcome,
    content_hash,
    new_id,
    utc_now,
)
from harness.tools.gateway import after_seconds

TERMINAL = {"completed", "failed", "cancelled"}
ALLOWED = {
    "queued": {"running", "cancelled"},
    "running": {
        "waiting_user",
        "waiting_children",
        "recovering",
        "needs_review",
        "completed",
        "failed",
        "cancelling",
    },
    "waiting_user": {"queued", "cancelled", "cancelling"},
    "waiting_children": {"queued", "cancelling"},
    "recovering": {"queued", "needs_review", "cancelling"},
    "needs_review": {"queued", "cancelling"},
    "cancelling": {"cancelled"},
}


class SchedulerConfig(BaseModel):
    worker_slots: int = Field(default=4, ge=1, le=32)
    model_concurrency: int = Field(default=4, ge=1, le=32)
    child_concurrency: int = Field(default=2, ge=1, le=16)
    max_depth: int = Field(default=2, ge=0, le=8)
    lease_seconds: float = 30
    heartbeat_seconds: float = 5
    scan_seconds: float = 0.25


class BudgetPolicy(BaseModel):
    max_model_calls: int = 100
    max_total_tokens: int = 1000000
    max_cost: float | None = None
    max_wall_seconds: int = 3600
    max_tool_calls: int = 100
    max_children: int = 8
    currency: str = "USD"
    estimate_policy: str = "reserve_upper_bound"

    def limits(self):
        return dict(
            model_calls=self.max_model_calls,
            total_tokens=self.max_total_tokens,
            cost=self.max_cost,
            tool_calls=self.max_tool_calls,
            children=self.max_children,
        )


class Scheduler:
    def __init__(self, store, policy, artifacts, config: SchedulerConfig | None = None):
        self.store, self.policy, self.artifacts = store, policy, artifacts
        self.config = config or SchedulerConfig()
        self.gateway = None
        self.processes = None
        self.cancel_callback = None
        self.snapshot_factory = None
        self.run_created_hook = None
        self.terminal_hook = None

    def _event_state(self, run: dict, event="run.state_changed", reason=None):
        self.store.event(
            run["id"],
            event,
            dict(
                revision=run["revision"],
                status=run["status"],
                reason=reason,
                result_ref=run["result_ref"],
                error=run["error"],
            ),
        )

    def transition(self, run: dict, status: str, *, ctx=None, reason=None, extra=None) -> dict:
        if status not in ALLOWED.get(run["status"], set()):
            raise HarnessError("INVALID_TRANSITION", f"不允许从 {run['status']} 转为 {status}")
        with self.store.transaction():
            updated = self.store.update_run(
                run["id"], run["revision"], dict(status=status, **(extra or {})), ctx
            )
            if status in TERMINAL:
                branch = self.store.branch(run["branch_id"])
                if branch["active_run_id"] == run["id"]:
                    self.store.set_branch_owner(branch["id"], run["id"], None)
                self.store.revoke_grants(run["id"])
                for interaction in self.store.interactions(run["id"]):
                    if interaction["status"] in {"pending_binding", "open"}:
                        closed = self.store.update_interaction(
                            interaction["id"],
                            interaction["revision"],
                            dict(
                                status="cancelled",
                                resolution=dict(
                                    interrupt_id=interaction["interrupt_id"],
                                    outcome="cancelled",
                                    reason="run_terminal",
                                ),
                            ),
                        )
                        self.store.event(
                            run["id"],
                            "interaction.resolved",
                            dict(
                                interaction_id=interaction["id"],
                                revision=closed["revision"],
                                resolution="cancelled",
                            ),
                        )
                for operation in self.store.operations(run["id"]):
                    if operation["state"] in {"prepared", "authorized"}:
                        from harness.tools.contracts import ToolResult

                        result = ToolResult(
                            operation_id=operation["id"],
                            status="cancelled",
                            summary="任务已结束，操作不再继续",
                            error_code="RUN_TERMINAL",
                            completed_at=utc_now(),
                        )
                        self.store.update_operation(
                            operation["id"],
                            operation["revision"],
                            dict(state="cancelled", result=result.model_dump(mode="json")),
                        )
                        if self.store.reservation("tool:" + operation["id"]):
                            used = 1 if operation["tool_id"] in {"request_input", "wait_children"} else 0
                            self.store.settle("tool:" + operation["id"], {"tool_calls": used})
                        self.store.event(
                            run["id"],
                            "tool.completed",
                            dict(
                                operation_id=operation["id"],
                                tool_id=operation["tool_id"],
                                tool_revision=operation["tool_revision"],
                                result_status="cancelled",
                                result_summary=result.summary,
                                artifact_refs=[],
                            ),
                        )
                if self.terminal_hook:
                    self.terminal_hook(updated)
                if run["parent_run_id"]:
                    self.store.outbox(
                        "children",
                        run["id"],
                        dict(child_run_id=run["id"], parent_run_id=run["parent_run_id"]),
                    )
                    self.store.event(
                        run["parent_run_id"],
                        "child.completed",
                        dict(
                            child_run_id=run["id"],
                            goal_summary=run["input"].get("text", ""),
                            status=status,
                            result_ref=updated["result_ref"],
                        ),
                    )
            self._event_state(
                updated,
                "run." + status
                if status in TERMINAL
                else "run.started"
                if status == "running"
                else "run.queued"
                if status == "queued"
                else "run.state_changed",
                reason,
            )
            return updated

    def submit(self, owner: str, session_id: str, request: dict, snapshot: dict | None = None) -> dict:
        session = self.store.session(session_id, owner)
        maintenance = self.store.get("system", "maintenance") or {}
        if maintenance.get("enabled"):
            raise HarnessError("MAINTENANCE", "服务处于维护状态，暂停新任务", 503)
        branch_id = request.get("branch_id", session["default_branch_id"])
        branch = self.store.branch(branch_id)
        if branch["session_id"] != session_id:
            raise HarnessError("BRANCH_SCOPE", "分支不属于此会话", 404)
        if snapshot is None:
            if self.snapshot_factory is None:
                raise HarnessError("CONFIGURATION", "配置快照服务未连接", 503)
            snapshot = self.snapshot_factory(owner, session, request)
        agent = snapshot["agent_spec"]
        if request.get("agent_spec_revision", agent.get("revision", 1)) != agent.get("revision", 1):
            raise HarnessError("REVISION_CONFLICT", "Agent 模板已更新")
        budget = BudgetPolicy.model_validate(snapshot.get("budget") or agent.get("budget") or {})
        rid, sid, now = new_id(), snapshot.get("config_snapshot_id", new_id()), utc_now()
        parts = request.get("content_parts", [])
        text = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
        attachments = request.get("attachment_refs", [])
        for ref in attachments:
            row = self.store.artifact(ref["artifact_id"], owner)
            if row["workspace_id"] != session["workspace_id"]:
                raise HarnessError("ARTIFACT_SCOPE", "附件不属于当前工作区", 403)
            self.artifacts.path(ref["artifact_id"], owner)
        with self.store.transaction():
            revision = self.store.advance_branch(branch_id, request["expected_branch_revision"])
            self.store.put("snapshots", sid, {**snapshot, "config_snapshot_id": sid})
            run = self.store.insert_run(
                dict(
                    id=rid,
                    owner_id=owner,
                    workspace_id=session["workspace_id"],
                    session_id=session_id,
                    branch_id=branch_id,
                    root_run_id=rid,
                    parent_run_id=None,
                    status="queued",
                    revision=1,
                    input_revision=revision,
                    snapshot_id=sid,
                    input=dict(text=text, content_parts=parts, attachment_refs=attachments),
                    deadline_at=after_seconds(budget.max_wall_seconds),
                    created_at=now,
                    updated_at=now,
                    depth=0,
                )
            )
            self.store.add_account(rid, "root", rid, budget.limits())
            if self.run_created_hook:
                self.run_created_hook(run, snapshot)
            message_id = new_id()
            self.store.add_message(
                rid, message_id, "user", dict(message_id=message_id, role="user", content_parts=parts)
            )
            for ref in attachments:
                self.store.reference_artifact(ref["artifact_id"], "run", rid)
            self._event_state(run, "run.queued")
        return dict(
            run_id=rid,
            branch_id=branch_id,
            branch_revision=revision,
            status=run["status"],
            revision=run["revision"],
        )

    def claim(
        self, worker_id: str, pid: int | None = None, process_start: float | None = None
    ) -> ExecutionContext | None:
        maintenance = self.store.get("system", "maintenance") or {}
        if maintenance.get("enabled"):
            return None
        pid = pid or os.getpid()
        process_start = process_start or psutil.Process(pid).create_time()
        with self.store.transaction():
            running = self.store.runs(statuses=["running", "cancelling"])
            if len(running) >= self.config.worker_slots:
                return None
            for run in self.store.runs(statuses=["queued"]):
                branch = self.store.branch(run["branch_id"])
                if branch["active_run_id"] not in {None, run["id"]}:
                    continue
                if run["cancel_requested"]:
                    self.transition(run, "cancelled", reason="已取消")
                    continue
                if run["parent_run_id"]:
                    siblings = [
                        r for r in running if r["root_run_id"] == run["root_run_id"] and r["parent_run_id"]
                    ]
                    if len(siblings) >= self.config.child_concurrency:
                        continue
                # FIFO for multiple unclaimed tasks on a branch is provided by store ordering.
                self.store.set_branch_owner(branch["id"], branch["active_run_id"], run["id"])
                attempt_id, fence = new_id(), run["fencing_token"] + 1
                self.store.add_attempt(
                    dict(
                        id=attempt_id,
                        run_id=run["id"],
                        worker_id=worker_id,
                        pid=pid,
                        process_start=process_start,
                        lease_expiry=after_seconds(self.config.lease_seconds),
                        fencing_token=fence,
                    )
                )
                run = self.transition(run, "running", extra=dict(attempt_id=attempt_id, fencing_token=fence))
                return self.context(run)
        return None

    def context(self, run: dict) -> ExecutionContext:
        branch = self.store.branch(run["branch_id"])
        return ExecutionContext(
            owner_id=run["owner_id"],
            workspace_id=run["workspace_id"],
            session_id=run["session_id"],
            branch_id=run["branch_id"],
            run_id=run["id"],
            root_run_id=run["root_run_id"],
            parent_run_id=run["parent_run_id"],
            worker_attempt_id=run["attempt_id"],
            fencing_token=run["fencing_token"],
            graph_thread_key=branch["graph_thread_key"],
            input_revision=run["input_revision"],
            config_snapshot_id=run["snapshot_id"],
            deadline_at=run["deadline_at"],
            trace_id=run["root_run_id"],
        )

    def heartbeat(self, ctx):
        self.store.heartbeat(ctx, after_seconds(self.config.lease_seconds))

    def boundary(self, ctx):
        self.store.assert_fence(ctx)
        if ctx.deadline_at and ctx.deadline_at <= utc_now():
            raise HarnessError("DEADLINE_EXCEEDED", "任务已超过总时限")

    def settle(self, ctx: ExecutionContext, outcome: RuntimeOutcome):
        if outcome.run_id != ctx.run_id or outcome.input_revision != ctx.input_revision:
            raise HarnessError("OUTCOME_MISMATCH", "运行结果与当前输入版本不对应")
        with self.store.transaction():
            self.store.assert_fence(ctx, allow_cancelling=True)
            run = self.store.run(ctx.run_id)
            checkpoint = outcome.checkpoint_ref.model_dump(mode="json") if outcome.checkpoint_ref else None
            if checkpoint:
                run = self.store.update_run(run["id"], run["revision"], dict(checkpoint_ref=checkpoint), ctx)
            unresolved = [
                x
                for x in self.store.operations(ctx.run_id)
                if x["state"] in {"running", "unknown"}
                and not (x["metadata"] or {}).get("unresolved_accepted")
            ]
            if run["status"] == "cancelling":
                if not unresolved and all(x["status"] in TERMINAL for x in self.store.children(run["id"])):
                    run = self.transition(run, "cancelled", ctx=ctx, reason="执行者已退出；已有副作用保留")
                self.store.end_attempt(ctx.worker_attempt_id, "cancel")
                return run
            if outcome.kind == "waiting":
                if not checkpoint:
                    raise HarnessError("CHECKPOINT_REQUIRED", "等待必须关联持久 checkpoint")
                required = []
                kinds = set()
                for ref in outcome.interrupt_refs:
                    required.append(ref.interrupt_id)
                    kinds.add(ref.kind)
                    if ref.kind == "user":
                        interaction = self.store.interaction(ref.interaction_id)
                        if (
                            interaction["run_id"] != run["id"]
                            or interaction["input_revision"] != ctx.input_revision
                        ):
                            raise HarnessError("INTERRUPT_MISMATCH", "交互不属于当前运行版本")
                        if interaction["status"] == "pending_binding":
                            interaction = self.store.update_interaction(
                                interaction["id"],
                                interaction["revision"],
                                dict(interrupt_id=ref.interrupt_id, checkpoint_ref=checkpoint, status="open"),
                            )
                            self.store.event(
                                run["id"],
                                "interaction.required",
                                {
                                    k: interaction.get(k)
                                    for k in (
                                        "interaction_id",
                                        "revision",
                                        "kind",
                                        "prompt",
                                        "response_schema",
                                        "binding_ref",
                                        "expires_at",
                                    )
                                },
                            )
                    elif ref.kind == "children":
                        wait = self.store.wait(ref.wait_id)
                        self.store.update_wait(
                            wait["id"], wait["revision"], ref.interrupt_id, checkpoint, wait["resolution"]
                        )
                    elif ref.kind == "review" and ref.operation_id:
                        operation = self.store.operation(ref.operation_id)
                        if operation["state"] == "running":
                            self.store.update_operation(
                                operation["id"], operation["revision"], dict(state="unknown")
                            )
                status = (
                    "needs_review"
                    if "review" in kinds
                    else "waiting_user"
                    if "user" in kinds
                    else "waiting_children"
                )
                run = self.transition(run, status, ctx=ctx, extra=dict(required_interrupts=required))
                self.store.end_attempt(ctx.worker_attempt_id, "waiting")
                self._refresh_waits(run["id"])
                self._wake(run["id"])
                return self.store.run(run["id"])
            if unresolved:
                run = self.transition(run, "needs_review", ctx=ctx, reason="仍有结果未知的操作")
            elif outcome.kind == "final":
                if not checkpoint or not outcome.result_ref:
                    raise HarnessError("FINAL_NOT_DURABLE", "最终结果缺少持久位置")
                self.artifacts.path(outcome.result_ref.artifact_id, ctx.owner_id)
                if any(c["status"] not in TERMINAL for c in self.store.children(ctx.run_id)):
                    raise HarnessError("CHILDREN_UNSETTLED", "子任务尚未结算，父任务不能完成")
                run = self.transition(
                    run,
                    "completed",
                    ctx=ctx,
                    extra=dict(result_ref=outcome.result_ref.model_dump(mode="json")),
                )
            else:
                error = (
                    outcome.error.model_dump(mode="json")
                    if outcome.error
                    else dict(
                        code="RUNTIME_STOPPED",
                        message="运行内核已停止",
                        retryable=False,
                        correlation_id=new_id(),
                    )
                )
                run = self.transition(run, "failed", ctx=ctx, extra=dict(error=error))
            self.store.end_attempt(ctx.worker_attempt_id, outcome.kind)
            return run

    def resolutions(self, run_id: str) -> dict:
        resolutions = {
            i["interrupt_id"]: i["resolution"]
            for i in self.store.interactions(run_id)
            if i["interrupt_id"] and i["resolution"]
        }
        resolutions.update(
            {
                w["interrupt_id"]: w["resolution"]
                for w in self.store.waits(run_id)
                if w["interrupt_id"] and w["resolution"]
            }
        )
        resolutions.update((self.store.get("review_resolutions", run_id) or {}).get("values", {}))
        return resolutions

    def _wake(self, run_id: str):
        run = self.store.run(run_id)
        if run["status"] not in {"waiting_user", "waiting_children"} or run["cancel_requested"]:
            return
        ready = self.resolutions(run_id)
        if set(run["required_interrupts"]) <= ready.keys():
            self.transition(run, "queued", reason="当前 checkpoint 的全部恢复条件已满足")

    def respond(self, owner: str, interaction_id: str, request: dict) -> dict:
        with self.store.transaction():
            interaction = self.store.interaction(interaction_id)
            run = self.store.run(interaction["run_id"], owner)
            if interaction["revision"] != request["expected_revision"]:
                raise HarnessError("REVISION_CONFLICT", "交互已发生变化")
            if interaction["status"] != "open" or run["status"] != "waiting_user":
                raise HarnessError("INTERACTION_CLOSED", "此交互不接受答复")
            if interaction["expires_at"] and interaction["expires_at"] <= utc_now():
                raise HarnessError("INTERACTION_EXPIRED", "交互已过期", 410)
            try:
                jsonschema.validate(request["response"], interaction["response_schema"])
            except jsonschema.ValidationError as exc:
                raise HarnessError("RESPONSE_SCHEMA", "答复不符合交互要求", 422) from exc
            if interaction["kind"] == "approval" and request.get("binding_ref") != interaction.get(
                "binding_ref"
            ):
                raise HarnessError("BINDING_MISMATCH", "审批参数绑定不匹配", 409)
            resolution = dict(
                interrupt_id=interaction["interrupt_id"],
                outcome="answered",
                response=request["response"],
                reason="user_response",
            )
            interaction = self.store.update_interaction(
                interaction_id, interaction["revision"], dict(status="resolved", resolution=resolution)
            )
            self.store.event(
                run["id"],
                "interaction.resolved",
                dict(interaction_id=interaction_id, revision=interaction["revision"], resolution="answered"),
            )
            self._wake(run["id"])
            return dict(
                interaction_id=interaction_id,
                run_id=run["id"],
                status=self.store.run(run["id"])["status"],
                revision=interaction["revision"],
                accepted=True,
            )

    def expire_interactions(self):
        with self.store.transaction():
            for run in self.store.runs(statuses=["waiting_user", "waiting_children"]):
                for interaction in self.store.interactions(run["id"]):
                    if (
                        interaction["status"] == "open"
                        and interaction["expires_at"]
                        and interaction["expires_at"] <= utc_now()
                    ):
                        resolution = dict(
                            interrupt_id=interaction["interrupt_id"],
                            outcome="expired",
                            reason="interaction_deadline",
                        )
                        updated = self.store.update_interaction(
                            interaction["id"],
                            interaction["revision"],
                            dict(status="expired", resolution=resolution),
                        )
                        self.store.event(
                            run["id"],
                            "interaction.resolved",
                            dict(
                                interaction_id=interaction["id"],
                                revision=updated["revision"],
                                resolution="expired",
                            ),
                        )
                self._wake(run["id"])

    def cancel(self, owner: str, run_id: str, reason: str = "user_cancel") -> dict:
        with self.store.transaction():
            root = self.store.run(run_id, owner)
            descendants = [root]
            cursor = 0
            while cursor < len(descendants):
                descendants.extend(self.store.children(descendants[cursor]["id"]))
                cursor += 1
            # Mark the barrier before any child can be created.
            for run in descendants:
                if run["status"] in TERMINAL:
                    continue
                run = self.store.update_run(run["id"], run["revision"], dict(cancel_requested=1))
                self.store.revoke_grants(run["id"])
                for interaction in self.store.interactions(run["id"]):
                    if interaction["status"] in {"pending_binding", "open"}:
                        closed = self.store.update_interaction(
                            interaction["id"],
                            interaction["revision"],
                            dict(
                                status="cancelled",
                                resolution=dict(
                                    interrupt_id=interaction["interrupt_id"],
                                    outcome="cancelled",
                                    reason=reason,
                                ),
                            ),
                        )
                        self.store.event(
                            run["id"],
                            "interaction.resolved",
                            dict(
                                interaction_id=interaction["id"],
                                revision=closed["revision"],
                                resolution="cancelled",
                            ),
                        )
                if run["status"] == "queued" or (
                    run["status"] == "waiting_user"
                    and not self.store.children(run["id"])
                    and not any(
                        x["state"] in {"running", "unknown"} for x in self.store.operations(run["id"])
                    )
                ):
                    self.transition(run, "cancelled", reason=reason)
                elif run["status"] != "cancelling":
                    self.transition(run, "cancelling", reason=reason)
        for run in descendants:
            if self.processes:
                self.processes.cancel_run(run["id"])
            if self.cancel_callback:
                self.cancel_callback(run["id"])
        self.finish_idle_cancellations()
        result = self.store.run(run_id)
        return dict(run_id=run_id, status=result["status"], revision=result["revision"], accepted=True)

    def finish_idle_cancellations(self):
        with self.store.transaction():
            for run in reversed(self.store.runs(statuses=["cancelling"])):
                attempt = self.store.attempt(run["attempt_id"]) if run["attempt_id"] else None
                if attempt and not attempt["ended_at"]:
                    continue
                unresolved = any(
                    x["state"] in {"running", "unknown"}
                    and not (x["metadata"] or {}).get("unresolved_accepted")
                    for x in self.store.operations(run["id"])
                )
                if not unresolved and all(x["status"] in TERMINAL for x in self.store.children(run["id"])):
                    self.transition(run, "cancelled", reason="取消屏障已满足")

    def delegate(self, ctx: ExecutionContext, key: OperationKey, request: dict) -> dict:
        delegation_key = content_hash(key)
        args_hash = content_hash(request)
        with self.store.transaction():
            self.boundary(ctx)
            saved = self.store.dependency(delegation_key)
            if saved:
                if saved["args_hash"] != args_hash:
                    raise HarnessError("DELEGATION_CONFLICT", "同一委派操作的参数发生变化")
                return self.store.run(saved["child_id"])
            parent = self.store.run(ctx.run_id)
            root = self.store.run(ctx.root_run_id)
            if (
                root["cancel_requested"]
                or parent["cancel_requested"]
                or parent["depth"] >= self.config.max_depth
            ):
                raise HarnessError("DELEGATION_LIMIT", "委派已取消或超过深度限制")
            allowed = set(self.policy.effective(ctx)["capabilities"])
            requested = set(request.get("capabilities", allowed))
            if not requested <= allowed:
                raise HarnessError("CHILD_PERMISSION", "子任务权限不能超过父任务", 403)
            self.store.reserve(ctx.root_run_id, "child:" + delegation_key, {"children": 1})
            snapshot = dict(self.store.get("snapshots", ctx.config_snapshot_id))
            sid = new_id()
            snapshot["config_snapshot_id"] = sid
            snapshot["policy"] = {**snapshot["policy"], "capabilities": sorted(requested)}
            self.store.put("snapshots", sid, snapshot)
            branch = self.store.add_branch(parent["session_id"], None)
            rid, now = new_id(), utc_now()
            child = self.store.insert_run(
                dict(
                    id=rid,
                    owner_id=ctx.owner_id,
                    workspace_id=ctx.workspace_id,
                    session_id=ctx.session_id,
                    branch_id=branch["id"],
                    root_run_id=ctx.root_run_id,
                    parent_run_id=ctx.run_id,
                    status="queued",
                    revision=1,
                    input_revision=1,
                    snapshot_id=sid,
                    input=dict(
                        text=request["goal"] + "\n完成标准：" + request["completion_criteria"],
                        content_parts=[dict(type="text", text=request["goal"])],
                        attachment_refs=[],
                    ),
                    deadline_at=parent["deadline_at"],
                    created_at=now,
                    updated_at=now,
                    depth=parent["depth"] + 1,
                )
            )
            if self.run_created_hook:
                self.run_created_hook(child, snapshot)
            self.store.add_dependency(ctx.run_id, rid, delegation_key, args_hash)
            self.store.settle("child:" + delegation_key, {"children": 1})
            self.store.event(
                ctx.run_id,
                "child.created",
                dict(child_run_id=rid, goal_summary=request["goal"], status="queued", result_ref=None),
            )
            self._event_state(child, "run.queued")
            return child

    def prepare_wait(self, ctx, key: str, children: list[str], mode: str):
        known = {x["id"] for x in self.store.children(ctx.run_id)}
        if not set(children) <= known or not children or mode not in {"all", "any"}:
            raise HarnessError("WAIT_SCOPE", "只能等待当前任务已派发的子任务", 422)
        return self.store.add_wait(ctx.run_id, key, children, mode)

    def _refresh_waits(self, parent: str | None = None):
        for wait in self.store.waits(parent):
            if not wait["checkpoint_ref"] or wait["resolution"]:
                continue
            children = [self.store.run(x) for x in wait["child_ids"]]
            statuses = [x["status"] in TERMINAL for x in children]
            if all(statuses) if wait["mode"] == "all" else any(statuses):
                resolution = dict(
                    interrupt_id=wait["interrupt_id"],
                    outcome="answered",
                    reason="children_ready",
                    children=[
                        dict(run_id=c["id"], status=c["status"], result_ref=c["result_ref"], error=c["error"])
                        for c in children
                    ],
                )
                self.store.update_wait(
                    wait["id"], wait["revision"], wait["interrupt_id"], wait["checkpoint_ref"], resolution
                )
                self._wake(wait["parent_id"])

    def refresh_waits(self):
        with self.store.transaction():
            self._refresh_waits()
            for item in self.store.pending_outbox("children"):
                self._refresh_waits(item["payload"]["parent_run_id"])
                self.store.deliver_outbox(item["id"])

    def recover(self):
        reports = []
        for run in self.store.runs(statuses=["running", "recovering", "cancelling"]):
            attempt = self.store.attempt(run["attempt_id"]) if run["attempt_id"] else None
            if not attempt or attempt["lease_expiry"] > utc_now():
                continue
            try:
                process = psutil.Process(attempt["pid"])
                alive = abs(process.create_time() - attempt["process_start"]) < 0.01 and process.is_running()
            except psutil.Error:
                alive = False
            if not alive and self.processes:
                self.processes.recover_run(run["id"])
            with self.store.transaction():
                run = self.store.run(run["id"])
                if run.get("attempt_id") != attempt["id"]:
                    continue
                if run["status"] == "running":
                    run = self.transition(run, "recovering", reason="执行租约失联")
                continuations = set()
                for operation in self.store.operations(run["id"]):
                    pending = (operation.get("metadata") or {}).get("protocol_continuation") or {}
                    if (
                        operation["state"] == "running"
                        and pending.get("kind") == "modern_mrtr"
                        and pending.get("status") == "awaiting_input"
                        and all(
                            pending.get(k)
                            for k in ("request_state_ref", "interaction_id", "schema_hash", "request_hash")
                        )
                    ):
                        try:
                            interaction = self.store.interaction(pending["interaction_id"])
                            if (
                                interaction["run_id"] == run["id"]
                                and interaction.get("operation_id") == operation["id"]
                            ):
                                continuations.add(operation["id"])
                        except HarnessError:
                            pass
                self.store.revoke_grants(
                    run["id"],
                    except_ids=[
                        op["grant_id"]
                        for op in self.store.operations(run["id"])
                        if op["id"] in continuations and op.get("grant_id")
                    ],
                )
                if alive:
                    reports.append(dict(run_id=run["id"], state="waiting_old_worker_exit"))
                    continue
                self.store.end_attempt(attempt["id"], "worker_lost")
                unknown = False
                for operation in self.store.operations(run["id"]):
                    if operation["state"] != "running":
                        unknown |= operation["state"] == "unknown"
                        continue
                    metadata = operation["metadata"] or {}
                    if operation["id"] in continuations:
                        # Resume the saved exchange, never the initial tools/call.
                        continue
                    if metadata.get("protocol_continuation"):
                        self.store.update_operation(
                            operation["id"], operation["revision"], dict(state="unknown")
                        )
                        unknown = True
                        continue
                    if (
                        operation["tool_id"] in {"write_file", "apply_patch"}
                        and metadata.get("path")
                        and Path(metadata["path"]).is_file()
                    ):
                        with Path(metadata["path"]).open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if digest == metadata.get("after_hash"):
                            from harness.tools.contracts import ToolResult

                            result = ToolResult(
                                operation_id=operation["id"],
                                status="succeeded",
                                summary="文件目标 hash 已验证，已完成写入",
                                structured_data=dict(path=operation["args"]["path"], sha256=digest),
                                completed_at=utc_now(),
                            )
                            self.store.update_operation(
                                operation["id"],
                                operation["revision"],
                                dict(state="succeeded", result=result.model_dump(mode="json")),
                            )
                            continue
                    spec, _ = self.gateway.registry.get(operation["tool_id"], operation["tool_revision"])
                    if spec.effect == "read":
                        self.store.update_operation(
                            operation["id"], operation["revision"], dict(state="prepared", grant_id=None)
                        )
                    else:
                        self.store.update_operation(
                            operation["id"], operation["revision"], dict(state="unknown")
                        )
                        unknown = True
                if run["status"] == "recovering":
                    self.transition(
                        run,
                        "needs_review" if unknown else "queued",
                        reason="未知副作用需要核对" if unknown else "旧执行者退出，允许从持久位置恢复",
                    )
                reports.append(dict(run_id=run["id"], state=self.store.run(run["id"])["status"]))
        self.finish_idle_cancellations()
        return reports

    def reconcile(self, owner: str, operation_id: str, request: dict):
        with self.store.transaction():
            operation = self.store.operation(operation_id)
            run = self.store.run(operation["run_id"], owner)
            if run["status"] not in {"needs_review", "cancelling"}:
                raise HarnessError("RUN_NOT_REVIEWING", "任务未处于可核对状态")
            result = self.gateway.reconcile(operation_id, owner, request)
            unknown = [
                x
                for x in self.store.operations(run["id"])
                if x["state"] == "unknown" and not x["metadata"].get("unresolved_accepted")
            ]
            if not unknown and result["allowed_action"] == "resume" and run["status"] == "needs_review":
                self.store.put(
                    "review_resolutions",
                    run["id"],
                    dict(
                        values={
                            x: dict(interrupt_id=x, outcome="answered", reason="operation_reconciled")
                            for x in run["required_interrupts"]
                        }
                    ),
                )
                self.transition(run, "queued", reason="已记录可恢复核对结论")
            elif result["allowed_action"] == "end_without_success" and run["status"] == "needs_review":
                self.transition(
                    run, "cancelling", reason="用户接受未决后果并停止任务", extra=dict(cancel_requested=1)
                )
        self.finish_idle_cancellations()
        return {**result, "status": self.store.run(run["id"])["status"], "accepted": True}

    def steer(self, owner: str, run_id: str, text: str, expected_input_revision: int):
        with self.store.transaction():
            run = self.store.run(run_id, owner)
            if run["status"] not in {"running", "queued"} or run["cancel_requested"]:
                raise HarnessError("STEERING_UNAVAILABLE", "当前运行状态不能接收追加指令")
            pending = self.store.pending_input_commands(run_id)
            latest = max([run["input_revision"]] + [x["input_revision"] for x in pending])
            if latest != expected_input_revision:
                raise HarnessError("REVISION_CONFLICT", "输入版本已更新")
            revision = latest + 1
            return self.store.add_input_command(run_id, revision, text)

    def validate_branch_source(
        self,
        owner: str,
        session_id: str,
        checkpoint: dict,
        expected_branch_revision: int,
        side_effect_policy: str,
    ):
        self.store.session(session_id, owner)
        if side_effect_policy != "preserve_external":
            raise HarnessError("SIDE_EFFECT_POLICY", "分支必须明确保留既有外部副作用", 422)
        source = next(
            (
                x
                for x in self.store.branches(session_id)
                if x["graph_thread_key"] == checkpoint["graph_thread_key"]
            ),
            None,
        )
        if not source or source["revision"] != expected_branch_revision:
            raise HarnessError("REVISION_CONFLICT", "源分支已变化或不可访问")
        matching = [
            r
            for r in self.store.runs(session_id=session_id)
            if r["branch_id"] == source["id"] and r["checkpoint_ref"] == checkpoint
        ]
        if not matching or source["active_run_id"]:
            raise HarnessError("CHECKPOINT_UNAVAILABLE", "只能从已保存且无活动任务的分支点创建分支")
        return source

    def create_branch(
        self,
        owner: str,
        session_id: str,
        checkpoint: dict,
        expected_branch_revision: int,
        side_effect_policy: str,
        *,
        graph_thread_key=None,
        copied_checkpoint=None,
    ):
        with self.store.transaction():
            source = self.validate_branch_source(
                owner, session_id, checkpoint, expected_branch_revision, side_effect_policy
            )
            branch = self.store.add_branch(
                session_id, source["id"], copied_checkpoint or checkpoint, graph_thread_key=graph_thread_key
            )
            pins = self.store.get("context_pins", source["id"])
            if pins:
                self.store.put("context_pins", branch["id"], {**pins, "revision": 1})
            return {
                **branch,
                "side_effect_policy": "preserve_external",
                "external_effects_rolled_back": False,
            }
