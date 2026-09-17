"""Transactional root budgets and cross-process model permits."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import psutil

from harness.core import DiagnosticContext, HarnessError, utc_now


class ModelBudgetAdapter:
    def __init__(self, store, scheduler=None, *, concurrency=4, permit_timeout=120):
        self.store, self.scheduler = store, scheduler
        self.concurrency, self.permit_timeout = concurrency, permit_timeout
        self.pid = os.getpid()
        self.process_start = psutil.Process(self.pid).create_time()

    def _boundary(self, ctx):
        if isinstance(ctx, DiagnosticContext):
            if ctx.deadline_at <= utc_now():
                raise HarnessError("DIAGNOSTIC_EXPIRED", "诊断期限已过")
        elif self.scheduler:
            self.scheduler.boundary(ctx)
        else:
            self.store.assert_fence(ctx)

    def _live(self, permit):
        if permit.get("released"):
            return False
        try:
            process = psutil.Process(permit["pid"])
            return process.is_running() and abs(process.create_time() - permit["process_start"]) < 0.01
        except psutil.Error:
            return False

    async def reserve(self, ctx, attempt_id, input_tokens, output_tokens):
        account_id = ctx.budget_account_id if isinstance(ctx, DiagnosticContext) else ctx.root_run_id
        key = "model:" + attempt_id
        expires = (
            (datetime.now(UTC) + timedelta(seconds=self.permit_timeout)).isoformat().replace("+00:00", "Z")
        )
        while True:
            self._boundary(ctx)
            acquired = False
            with self.store.transaction():
                permits = self.store.list("model_permits")
                active = [permit for permit in permits if self._live(permit)]
                if len(active) < self.concurrency:
                    amount = {"model_calls": 1, "total_tokens": input_tokens + (output_tokens or 0)}
                    account = self.store.account(account_id)
                    if account["limits"].get("cost") is not None:
                        snapshot = self.store.get("snapshots", ctx.config_snapshot_id) or {}
                        profile = snapshot.get("model_profile", {})
                        incoming, outgoing = (
                            profile.get("input_price_per_million"),
                            profile.get("output_price_per_million"),
                        )
                        if incoming is None or outgoing is None:
                            raise HarnessError(
                                "UNKNOWN_MODEL_PRICE", "已设置费用上限，但该模型尚无已核实价格"
                            )
                        amount["cost"] = (
                            input_tokens * incoming + (output_tokens or 0) * outgoing
                        ) / 1_000_000
                    self.store.reserve(account_id, key, amount)
                    self.store.put(
                        "model_permits",
                        key,
                        {
                            "key": key,
                            "pid": self.pid,
                            "process_start": self.process_start,
                            "account_id": account_id,
                            "expires_at": expires,
                            "released": False,
                        },
                    )
                    acquired = True
            if acquired:
                return {"key": key, "account_id": account_id, "amount": amount}
            await asyncio.sleep(0.05)

    async def settle(self, ctx, reservation, usage):
        if reservation is None:
            return
        key = reservation["key"]
        actual = None
        if usage.get("source") == "provider" and usage.get("total_tokens") is not None:
            actual = {"model_calls": 1, "total_tokens": usage["total_tokens"]}
            if "cost" in reservation["amount"]:
                if usage.get("cost") is None:
                    actual = None
                else:
                    actual["cost"] = usage["cost"]
        try:
            self.store.settle(key, actual)
        finally:
            # Permit lifetime follows completion of this actual network attempt.
            # Unknown money/token consumption remains reserved in the separate ledger.
            self.store.delete("model_permits", key)
