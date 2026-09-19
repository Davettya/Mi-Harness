"""Local, content-free metrics; execution state remains in its owning repositories."""

from datetime import datetime
from harness.core import utc_now


def elapsed_ms(start, end):
    return max(
        0,
        (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()
        * 1000,
    )


class MetricsService:
    def __init__(self, store):
        self.store = store

    def record(self, kind, data):
        # An allowlist excludes prompts, URLs, headers, arguments, deltas and secrets.
        key = data.get("model_attempt_id") or data.get("span_id")
        if not key:
            return
        with self.store.transaction():
            item = self.store.get("metrics", key) or {"started_at": utc_now()}
            if kind == "message.delta":
                if "first_token_ms" in item:
                    return
                item["first_token_ms"] = elapsed_ms(item["started_at"], utc_now())
            else:
                allowed = {
                    "model_attempt_id",
                    "run_id",
                    "diagnostic_id",
                    "trace_id",
                    "profile_ref",
                    "retry",
                    "code",
                    "retryable",
                    "duration_ms",
                    "first_token_ms",
                    "span_id",
                    "parent_span_id",
                    "phase", "status", "request_count",
                }
                item.update(
                    {
                        k: v
                        for k, v in data.items()
                        if k in allowed and isinstance(v, (str, int, float, bool, type(None)))
                    }
                )
                if isinstance(data.get("usage"), dict):
                    item["usage"] = {
                        k: v
                        for k, v in data["usage"].items()
                        if k in {"input_tokens", "output_tokens", "total_tokens", "cost", "source"}
                    }
                item["kind"] = kind
                if kind.endswith(("completed", "failed")):
                    item["duration_ms"] = data.get(
                        "duration_ms", data.get("latency_ms", elapsed_ms(item["started_at"], utc_now()))
                    )
            self.store.put("metrics", key, item)

    def snapshot(self):
        state = self.store.metrics_facts()
        records = self.store.list("metrics")
        models = [x for x in records if x.get("model_attempt_id")]
        durations = [x["duration_ms"] for x in models if "duration_ms" in x]
        first = [x["first_token_ms"] for x in models if "first_token_ms" in x]

        def summary(values):
            values = sorted(v for v in values if isinstance(v, (int, float)))
            return {
                "count": len(values),
                "p95": values[min(len(values) - 1, int(len(values) * 0.95))] if values else None,
            }

        state.update(
            timestamp=utc_now(),
            model_total_ms=summary(durations),
            model_first_token_ms=summary(first),
            model_attempts=len(models),
            retry_attempts=sum(bool(x.get("retry")) for x in models),
            usage_known=sum(x.get("usage", {}).get("source") == "provider" for x in models),
            usage_unknown=sum(x.get("usage", {}).get("source", "unknown") != "provider" for x in models),
            cost_known=sum(x.get("usage", {}).get("cost") is not None for x in models),
            raw_content_included=False,
        )
        state["checkpoint_latency_ms"] = summary(
            [
                x["duration_ms"]
                for x in records
                if x.get("kind") == "checkpoint.completed" and "duration_ms" in x
            ]
        )
        import psutil

        alive = 0
        for session in self.store.list("process_sessions"):
            if session.get("status") not in {"cancelled", "unknown"}:
                continue
            for identity in [
                dict(pid=session["pid"], process_start=session["process_start"]),
                *session.get("members", []),
            ]:
                try:
                    process = psutil.Process(identity["pid"])
                    alive += int(
                        abs(process.create_time() - identity["process_start"]) < 0.01 and process.is_running()
                    )
                except psutil.Error:
                    pass
        state["cancelled_or_lost_processes_alive"] = alive
        return state
