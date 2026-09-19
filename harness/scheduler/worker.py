"""Independent local worker; browser lifetime never owns graph execution."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from harness.core import ErrorEnvelope, HarnessError, RuntimeOutcome, new_id, utc_now
from harness.runtime import LangChainAgentRuntime, tool_shell

from .budget import ModelBudgetAdapter

log = logging.getLogger(__name__)


class ObservedSqliteSaver(AsyncSqliteSaver):
    on_checkpoint = None

    async def aput(self, config, checkpoint, metadata, new_versions):
        started = time.monotonic()
        saved = await super().aput(config, checkpoint, metadata, new_versions)
        if self.on_checkpoint:
            self.on_checkpoint(checkpoint, (time.monotonic() - started) * 1000)
        return saved


@asynccontextmanager
async def build_runtime(services):
    """Own the actual durable SQLite saver and provider pool lifetimes."""
    async with ObservedSqliteSaver.from_conn_string(
        str(Path(services.settings.data_dir) / "checkpoints.db")
    ) as saver:
        await saver.setup()
        budget = ModelBudgetAdapter(
            services.store, services.scheduler, concurrency=services.scheduler.config.model_concurrency
        )
        services.models.reserve, services.models.settle = budget.reserve, budget.settle
        services.models.boundary_check = budget._boundary
        services.models.stream_output = True
        previous_model_events = services.models.event_sink

        async def model_events(kind, data):
            if kind == "message.delta":
                services.store.publish_ephemeral(
                    data["run_id"], {key: value for key, value in data.items() if key != "run_id"}
                )
            if previous_model_events:
                from harness.model_gateway.gateway import maybe_await

                await maybe_await(previous_model_events(kind, data))

        services.models.event_sink = model_events

        def tools_factory(ctx, spec):
            snapshot = services.store.get("snapshots", ctx.config_snapshot_id) or {}
            frozen = {item.get("id", item.get("name")): item for item in snapshot.get("tools", [])}
            tools = []
            from harness.runtime.modes import permitted
            for name in frozen:
                item = frozen.get(name)
                if not permitted(snapshot.get("mode", "react"), item, snapshot):
                    continue
                if item:
                    tool, _ = services.registry.get(name, str(item.get("version", "1")))
                    if tool.input_schema != item.get("input_schema", tool.input_schema):
                        raise HarnessError("TOOL_SCHEMA_DRIFT", "运行中的工具schema与锁定快照不一致")
                else:
                    tool, _ = services.registry.get(name)
                tools.append(tool_shell(tool.id, tool.description, tool.input_schema))
            return tools

        def message_commit(ctx, identity, role, payload):
            role = {"human": "user", "ai": "assistant"}.get(role, role)
            content = payload.get("content", "")
            parts = (
                ([{"type": "text", "text": content}] if content else [])
                if isinstance(content, str)
                else content
            )
            projected = {"message_id": identity, "role": role, "content_parts": parts}
            projected.update(payload.get("model_binding", {}))
            services.store.add_message(ctx.run_id, identity, role, projected)

        def event_sink(ctx, kind, data):
            # Internal trace facts are separate from public canonical message/run events.
            if kind in {"runtime.error", "runtime.exited"}:
                services.store.put(
                    "runtime_diagnostics", ctx.run_id, {"type": kind, "data": data, "timestamp": utc_now()}
                )

        runtime = LangChainAgentRuntime(
            saver,
            services.models,
            services.context,
            services.gateway,
            tools_factory=tools_factory,
            repository=services.store,
            snapshot_loader=lambda key: services.store.get("snapshots", key),
            artifact_writer=services.artifacts.writer,
            boundary_check=services.scheduler.boundary,
            message_commit=message_commit,
            event_sink=event_sink,
            input_loader=lambda ctx: services.store.pending_input_commands(ctx.run_id),
            input_ack=services.store.ack_input_commands,
            model_selection=services.selection,
        )
        services.runtime = runtime

        def checkpoint_observed(checkpoint, duration):
            run_id = checkpoint.get("channel_values", {}).get("harness_run_id")
            ctx = runtime._contexts.get(run_id)
            if ctx and hasattr(services, "metrics"):
                services.metrics.record(
                    "checkpoint.completed",
                    {
                        "span_id": checkpoint["id"],
                        "run_id": run_id,
                        "trace_id": ctx.trace_id,
                        "duration_ms": duration,
                    },
                )

        saver.on_checkpoint = checkpoint_observed
        try:
            yield runtime
        finally:
            await services.models.aclose()
            services.models.event_sink = previous_model_events
            services.runtime = None


class Worker:
    def __init__(self, services, *, instance_id=None):
        self.services = services
        self.store, self.scheduler = services.store, services.scheduler
        self.instance_id = instance_id or services.settings.instance_id
        self.worker_id = f"{self.instance_id}:{os.getpid()}:{new_id()}"
        self.active = {}
        self.contexts = {}
        self.stopping = asyncio.Event()
        self._last_heartbeat = 0.0

    def acknowledge(self):
        maintenance = self.store.get("system", "maintenance") or {}
        status = (
            "running"
            if self.active
            else "drained"
            if maintenance.get("enabled") or self.stopping.is_set()
            else "ready"
        )
        self.store.put(
            "system",
            "worker_state",
            {
                "instance_id": self.instance_id,
                "worker_id": self.worker_id,
                "pid": os.getpid(),
                "status": status,
                "active_run_ids": sorted(self.active),
                "heartbeat_at": utc_now(),
            },
        )

    async def _execute(self, ctx):
        try:
            services = self.services
            snapshot = await services.prepare_run(ctx)
            run = self.store.run(ctx.run_id)
            # A worker may die after a graph checkpoint but before the business checkpoint pointer.
            # Always inspect the saver by stable thread key before deciding to start a new turn.
            checkpoint = run.get("checkpoint_ref")
            if checkpoint is None:
                try:
                    checkpoint = (await services.runtime.get_checkpoint(ctx)).model_dump(mode="json")
                except HarnessError as exc:
                    if exc.code not in {"checkpoint_not_found", "checkpoint_mismatch"}:
                        raise
            if checkpoint:
                required = set(run.get("required_interrupts", []))
                resolutions = {
                    key: value
                    for key, value in self.scheduler.resolutions(ctx.run_id).items()
                    if key in required
                }
                outcome = await services.runtime.resume(ctx, checkpoint, resolutions)
            else:
                original = [
                    item
                    for item in self.store.messages(ctx.branch_id, ctx.run_id)
                    if item.get("role") == "user"
                ]
                identity = original[0].get("message_id") if original else new_id()
                parts = run["input"].get("content_parts") or [
                    {"type": "text", "text": run["input"].get("text", "")}
                ]
                # Preserve multimodal parts; plain text has the same public projection as submit.
                content = parts[0]["text"] if len(parts) == 1 and parts[0].get("type") == "text" else parts
                outcome = await services.runtime.start(
                    ctx, snapshot, [HumanMessage(content=content, id=identity)]
                )
            while True:
                # Admission and terminal settlement serialize on the same SQLite write lock.
                with self.store.transaction():
                    current = self.store.run(ctx.run_id)
                    ctx = self.scheduler.context(current)
                    continue_pending = outcome.kind == "final" and bool(
                        self.store.pending_input_commands(ctx.run_id)
                    )
                    if not continue_pending:
                        self.scheduler.settle(ctx, outcome)
                        break
                outcome = await services.runtime.continue_input(ctx)
        except Exception as exc:  # noqa: BLE001 - worker must settle unexpected framework failures
            log.error("Worker task failed run=%s category=%s", ctx.run_id, type(exc).__name__)
            try:
                current = self.store.run(ctx.run_id)
                current_ctx = self.scheduler.context(current)
                error = ErrorEnvelope(
                    code=getattr(exc, "code", "WORKER_ERROR"),
                    message=str(exc)
                    if isinstance(exc, HarnessError)
                    else f"Worker failed ({type(exc).__name__})",
                )
                self.scheduler.settle(
                    current_ctx,
                    RuntimeOutcome(
                        kind="error",
                        run_id=ctx.run_id,
                        input_revision=current_ctx.input_revision,
                        error=error,
                    ),
                )
            except HarnessError:
                log.warning("Worker settlement rejected for superseded execution run=%s", ctx.run_id)
        finally:
            self.contexts.pop(ctx.run_id, None)
            if self.services.runtime:
                self.services.runtime.release_run(ctx.run_id)
            if self.store.run(ctx.run_id)["status"] in {"completed", "failed", "cancelled"}:
                try:
                    await self.services.mcp.close_scope(ctx.run_id)
                except Exception as exc:  # noqa: BLE001 - cleanup failure must not undo terminal settlement or kill other runs
                    log.error("MCP scope cleanup failed run=%s category=%s", ctx.run_id, type(exc).__name__)

    async def tick(self):
        self.store.publish_event_outbox()
        self.scheduler.expire_interactions()
        self.scheduler.refresh_waits()
        self.scheduler.recover()
        self.scheduler.finish_idle_cancellations()
        for run_id, task in list(self.active.items()):
            if task.done():
                await task
                del self.active[run_id]
        for run_id, ctx in list(self.contexts.items()):
            run = self.store.run(run_id)
            if run["cancel_requested"] and self.services.runtime:
                self.services.processes.cancel_run(run_id)
                await self.services.runtime.request_cancel(ctx)
        now = asyncio.get_running_loop().time()
        if now - self._last_heartbeat >= self.scheduler.config.heartbeat_seconds:
            for ctx in list(self.contexts.values()):
                try:
                    self.scheduler.heartbeat(ctx)
                except HarnessError:
                    if self.services.runtime:
                        await self.services.runtime.request_cancel(ctx)
            self._last_heartbeat = now
        maintenance = (self.store.get("system", "maintenance") or {}).get("enabled")
        while (
            not self.stopping.is_set()
            and not maintenance
            and len(self.active) < self.scheduler.config.worker_slots
        ):
            ctx = self.scheduler.claim(self.worker_id)
            if ctx is None:
                break
            self.contexts[ctx.run_id] = ctx
            self.active[ctx.run_id] = asyncio.create_task(self._execute(ctx))
        self.acknowledge()

    async def run(self):
        try:
            while not self.stopping.is_set() or self.active:
                await self.tick()
                await asyncio.sleep(self.scheduler.config.scan_seconds)
        finally:
            self.acknowledge()

    async def drain(self):
        self.stopping.set()
        if self.active:
            await asyncio.gather(*self.active.values())
            self.active.clear()
        self.acknowledge()


async def serve(data_dir: Path, instance_id: str):
    from harness.platform.config import instance_config
    from harness.server.composition import Services

    settings = instance_config(data_dir, instance_id)
    logging.getLogger().setLevel(settings.log_level)
    services = Services(settings)
    try:
        await services.open()
        async with build_runtime(services):
            worker = Worker(services, instance_id=instance_id)
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, worker.stopping.set)
                except NotImplementedError:
                    signal.signal(signum, lambda *_: loop.call_soon_threadsafe(worker.stopping.set))
            await worker.run()
    finally:
        await services.close()


def main():
    parser = argparse.ArgumentParser(description="Mi Harness worker")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(args.data_dir, args.instance_id))


if __name__ == "__main__":
    main()
