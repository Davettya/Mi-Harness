"""TB2.1 remaining cohort with explicitly authorized, evidence-bound recovery."""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import time
import tomllib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import portalocker
import psutil

import terminal_smoke as smoke


def now():
    return datetime.now(UTC).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def read(path, default=None):
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def classify(runtime, reward, verifier_exit, budget_exhausted=False):
    code = (runtime.get("error") or {}).get("code")
    if budget_exhausted:
        cause = "budget_exhausted"
    elif code in {"deadline_exceeded", "step_limit", "repeated_tool_failure", "context_limit"}:
        cause = "agent_limit"
    elif code:
        # Preserve exact error; unknown failures require evidence-based manual triage.
        cause = "runtime_error_needs_attribution"
    elif reward is None:
        cause = "verifier_timeout" if verifier_exit == 124 else "verifier_unscored"
    else:
        cause = "task_success" if reward > 0 else "task_failure"
    return {"status": "unscored" if reward is None else "passed" if reward > 0 else "failed",
            "cause": cause, "runtime_error_code": code}


async def logged(argv, path, timeout, stdin=None):
    """Stream external setup/verifier logs to disk, including on timeouts."""
    started = time.monotonic()
    with path.open("wb") as log:
        proc = await asyncio.create_subprocess_exec(
            *map(str, argv), stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=log, stderr=asyncio.subprocess.STDOUT)
        try:
            await asyncio.wait_for(proc.communicate(stdin), timeout)
            code = proc.returncode
        except (TimeoutError, asyncio.CancelledError) as exc:
            proc.kill()
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            code = 124
    return {"exit_code": code, "seconds": round(time.monotonic() - started, 3)}


def freeze(source, output, pilot):
    rev = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if rev != smoke.COMMIT:
        raise RuntimeError("Benchmark commit changed")
    dirty = subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no", "--", "tasks"], text=True)
    if dirty.strip():
        raise RuntimeError("Benchmark worktree modified")
    all_tasks = sorted(p.name for p in (source / "tasks").iterdir() if p.is_dir())
    pilot_manifest = read(pilot / "manifest.json")
    assert pilot_manifest["commit"] == rev and set(pilot_manifest["tasks"]) == set(smoke.TASKS)
    for name in smoke.TASKS:
        attempts = sorted(pilot.glob(name + "--*/result.json"))
        assert attempts and read(attempts[-1])["reward"] == 1, name
    remaining = [name for name in all_tasks if name not in smoke.TASKS]
    assert len(all_tasks) == 89 and len(remaining) == 79
    tree = subprocess.check_output(["git", "-C", str(source), "ls-tree", "-r", rev, "--", "tasks"], text=True)
    manifest = {"created": now(), "commit": rev, "source": str(source), "tasks": remaining,
                "excluded_pilot_tasks": list(smoke.TASKS), "pilot_manifest_sha256": digest(pilot / "manifest.json"),
                "model": "deepseek-flash", "reasoning_effort": "high", "thinking": "enabled",
                "budget_cny": 25, "budget_accounting": "peak conservative", "concurrency": 1,
                "output_limit": 16384, "max_model_calls": 100, "retries": "none for non-harness failures",
                "price_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "price_checked_date": "2026-09-20", "peak_cny_per_million": {"cache_hit": .04, "cache_miss": 2, "output": 8}}
    old = read(output / "manifest.json")
    if old:
        for key in ("commit", "tasks", "pilot_manifest_sha256", "model", "reasoning_effort", "budget_cny"):
            assert old[key] == manifest[key], key
        assert (output / "source-tree.txt").read_text("utf-8") == tree
        return old
    (output / "source-tree.txt").write_text(tree, encoding="utf-8")
    save(output / "manifest.json", manifest)
    return manifest


def summarize(output, manifest, ledger, budget_stopped=False, recovery_tasks=()):
    rows = []
    calls = ledger.report()
    for name in manifest["tasks"]:
        attempts = sorted(output.glob(name + "--*/result.json"))
        record = read(attempts[-1]) if attempts else {
            "task": name, "status": "not_attempted_budget" if budget_stopped else "pending", "reward": None}
        if (budget_stopped and name in recovery_tasks and len(attempts) == 1
                and record.get("status") == "setup_blocked"):
            record = {**record, "status": "not_attempted_budget", "cause": "recovery_budget_exhausted"}
        task_calls = [c for c in calls["calls"] if c["task"].startswith(name + "--")]
        row = {**record, "attempts": len(attempts), "model_calls": len(task_calls),
               "cost_peak_cny": round(sum(c["charged_peak_cny"] if c["charged_peak_cny"] is not None else c["reserved_cny"] for c in task_calls), 6),
               "input_tokens": sum(c.get("usage", {}).get("prompt_tokens", 0) for c in task_calls),
               "output_tokens": sum(c.get("usage", {}).get("completion_tokens", 0) for c in task_calls)}
        rows.append(row)
    counts = Counter(r["status"] for r in rows)
    completed = sum(counts[s] for s in ("passed", "failed", "unscored", "setup_blocked"))
    summary = {"updated": now(), "planned": 79, "completed": completed, "counts": dict(counts),
               "fixed_denominator_verified_success_rate": counts["passed"] / 79,
               "scorable_success_rate": counts["passed"] / (counts["passed"] + counts["failed"]) if counts["passed"] + counts["failed"] else None,
               "budget": {k: v for k, v in calls.items() if k != "calls"}, "rows": rows}
    save(output / "summary.json", summary)
    save(output / "budget.json", calls)
    fields = ["task", "status", "cause", "reward", "attempts", "model_calls", "input_tokens", "output_tokens", "cost_peak_cny", "seconds", "runtime_error_code", "image_digest"]
    with (output / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# Terminal-Bench 2.1 remaining cohort", "", f"Updated: {summary['updated']}", "",
             f"Completed {completed}/79; counts: {dict(counts)}. Conservative spend {ledger.spent()/1e6:.6f}/25 CNY.", "",
             "This is a Runtime/container-adapter evaluation, not a leaderboard or Web E2E result. Null reward means unscored, not zero. Pilot results are separate. The user-authorized infrastructure recovery cohort preserves every original setup failure and never retries an Agent or scored outcome.", "",
             "| Task | Status | Cause | Reward | Model calls | Peak CNY |", "|---|---|---|---:|---:|---:|"]
    lines += [f"| {r['task']} | {r['status']} | {r.get('cause', '')} | {r.get('reward')} | {r['model_calls']} | {r['cost_peak_cny']:.6f} |" for r in rows]
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


async def trial(name, source, output, ledger, state, attempt=1):
    folder = output / f"{name}--{attempt:02d}"
    folder.mkdir(exist_ok=False)
    task = source / "tasks" / name
    config = tomllib.loads((task / "task.toml").read_text("utf-8"))
    env = config["environment"]
    container = "mi-remaining-" + name + f"-{attempt}"
    started = time.monotonic()
    record = {"task": name, "attempt": attempt, "container": container, "started": now(),
              "status": "setup_blocked", "reward": None, "cause": "setup_incomplete"}
    for script in (Path(__file__), Path(smoke.__file__)):
        (folder / script.name).write_bytes(script.read_bytes())
    save(folder / "config.json", config)
    save(folder / "attempt-start.json", record)
    state.update(task=name, phase="pull", attempt=attempt)
    running = False
    try:
        pull = await logged(["docker", "pull", env["docker_image"]], folder / "pull.log", env["build_timeout_sec"])
        if pull["exit_code"]:
            record.update(cause="image_pull_failed", setup=pull)
            return record
        state["phase"] = "container_start"
        dockerfile = (task / "environment" / "Dockerfile").read_text("utf-8")
        task_startup = bool(re.search(r"^(CMD|ENTRYPOINT)\s", dockerfile, re.MULTILINE))
        record["startup_policy"] = "task_defined_command" if task_startup else "sleep_infinity"
        launch = ["docker", "run", "-d", "--name", container, "--label", "mi-harness-remaining=true",
                  "--cpus", str(env["cpus"]), "--memory", f"{env['memory_mb']}m"]
        launch += [env["docker_image"]] if task_startup else ["--entrypoint", "sleep", env["docker_image"], "infinity"]
        result = await logged(launch, folder / "container-start.log", 120)
        if result["exit_code"]:
            record.update(cause="container_start_failed", setup=result)
            return record
        running = True
        code, image = await smoke.command("docker", "image", "inspect", env["docker_image"], "--format", "{{json .RepoDigests}}")
        record["image_digest"] = json.loads(image) if code == 0 else None
        state["phase"] = "agent"
        record["agent_started"] = now()
        print("START " + name, flush=True)
        try:
            record["runtime_kind"] = await smoke.run_agent(task, container, folder, ledger, config["agent"]["timeout_sec"])
        except Exception as exc:
            record["runtime_kind"] = "error"
            save(folder / "runtime-result.json", {"kind": "error", "error": {"code": type(exc).__name__, "message": str(exc)}})
        record["agent_finished"] = now()
        runtime = read(folder / "runtime-result.json", {})
        # The harness-owned timeout kills any outstanding Agent command before grading.
        if record["runtime_kind"] != "final":
            await logged(["docker", "exec", container, "pkill", "-TERM", "-f", "^timeout -k 3 "], folder / "agent-command-stop.log", 15)
        state["phase"] = "verifier"
        setup = await logged(["docker", "exec", container, "mkdir", "-p", "/tests", "/logs/verifier"], folder / "verifier-setup.log", 30)
        if setup["exit_code"]:
            record.update(status="unscored", cause="verifier_setup_failed")
            return record
        archive = subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(source), "archive", smoke.COMMIT, f"tasks/{name}/tests"])
        copied = await logged(["docker", "exec", "-i", container, "tar", "-xf", "-", "--strip-components=3", "-C", "/tests"], folder / "verifier-copy.log", 60, archive)
        if copied["exit_code"]:
            record.update(status="unscored", cause="verifier_copy_failed")
            return record
        verified = await logged(["docker", "exec", container, "timeout", "-k", "5", str(config["verifier"]["timeout_sec"]), "bash", "/tests/test.sh"], folder / "verifier.log", config["verifier"]["timeout_sec"] + 15)
        record["verifier_exit"] = verified["exit_code"]
        record["verifier_seconds"] = verified["seconds"]
        code, reward = await smoke.command("docker", "exec", container, "cat", "/logs/verifier/reward.txt", timeout=15)
        record["reward_raw"] = reward
        try:
            record["reward"] = float(reward.strip()) if code == 0 else None
        except ValueError:
            record["reward"] = None
        await logged(["docker", "cp", container + ":/logs/verifier", str(folder / "verifier")], folder / "verifier-collect.log", 90)
        record.update(classify(runtime, record["reward"], record["verifier_exit"], ledger.budget_exhausted))
        return record
    except Exception as exc:
        record.update(cause="orchestrator_error_needs_attribution", error_type=type(exc).__name__, error=str(exc))
        return record
    finally:
        state["phase"] = "settle"
        if running:
            await logged(["docker", "inspect", container], folder / "container-inspect.json", 30)
            await logged(["docker", "stop", "-t", "3", container], folder / "container-stop.log", 25)
        record.update(finished=now(), seconds=round(time.monotonic() - started, 3))
        record["evidence_sha256"] = {p.relative_to(folder).as_posix(): digest(p) for p in folder.rglob("*") if p.is_file() and p.suffix not in {".db", ".sqlite"} and not p.name.endswith(("-wal", "-shm"))}
        save(folder / "result.json", record)
        print(json.dumps(record, default=str), flush=True)


async def run(args):
    import harness
    source, output, pilot = args.source.resolve(), args.output.resolve(), args.pilot.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with portalocker.Lock(str(output / "run.lock"), timeout=0):
        manifest = freeze(source, output, pilot)
        package = Path(harness.__file__).parent
        assert "runtime-site" in str(package), "Use the rebuilt installed wheel"
        wheel = Path(__file__).resolve().parents[1] / "dist/mi_harness-0.1.0-py3-none-any.whl"
        import zipfile
        with zipfile.ZipFile(wheel) as z:
            for p in package.rglob("*.py"):
                assert p.read_bytes() == z.read("harness/" + p.relative_to(package).as_posix()), p
        provenance = {"installed_package": str(package), "wheel_sha256": digest(wheel), "python": platform.python_version(),
                      "platform": platform.platform(), "script_hashes": {p.name: digest(p) for p in [Path(__file__), Path(smoke.__file__)]},
                      "started": now(), "pid": os.getpid(), "process_create_time": psutil.Process().create_time()}
        old = read(output / "provenance.json")
        repair = read(output / "budget-repair-authorization.json", {})
        recovery = read(output / "environment-recovery-authorization.json", {})
        repaired_provenance = read(output / "provenance-budget-repair.json")
        if recovery:
            predecessor = repaired_provenance or old
            assert predecessor, "Missing predecessor provenance"
            assert recovery.get("from_scripts") == predecessor["script_hashes"], "Unapproved recovery predecessor"
            assert recovery.get("to_scripts") == provenance["script_hashes"], "Unapproved recovery runner"
            assert recovery.get("from_wheel") == predecessor["wheel_sha256"], "Unapproved recovery wheel predecessor"
            assert recovery.get("to_wheel") == provenance["wheel_sha256"], "Unapproved recovery wheel"
            assert recovery.get("budget_cny") == 25, "Recovery must retain the original budget"
            save(output / "provenance-environment-recovery.json", provenance)
        elif old and old["script_hashes"] != provenance["script_hashes"]:
            assert repair.get("from_scripts") == old["script_hashes"], "Unapproved previous runner"
            assert repair.get("to_scripts") == provenance["script_hashes"], "Unapproved repaired runner"
            assert repair.get("from_wheel") == old["wheel_sha256"], "Unapproved previous wheel"
            assert repair.get("to_wheel") == provenance["wheel_sha256"], "Unapproved repaired wheel"
            save(output / "provenance-budget-repair.json", provenance)
        if not old:
            save(output / "provenance.json", provenance)
        ledger = smoke.Ledger(output / "budget.sqlite", budget_cny=25)
        if repair:
            ledger.reconcile_legacy_unknown()
        recovery_tasks = validate_environment_recovery(output, manifest, ledger, recovery) if recovery else set()
        state = {**provenance, "status": "running", "task": None, "phase": "preflight"}

        async def heartbeat():
            while True:
                state["heartbeat"] = now()
                state["conservative_cny"] = ledger.spent() / 1e6
                save(output / "state.json", state)
                await asyncio.sleep(10)

        beat = asyncio.create_task(heartbeat())
        try:
            await logged(["docker", "info", "--format", "{{json .}}"], output / "docker-info.json", 30)
            summarize(output, manifest, ledger, recovery_tasks=recovery_tasks)
            for name in manifest["tasks"]:
                previous = sorted(output.glob(name + "--*/result.json"))
                task_calls = [c for c in ledger.report()["calls"] if c["task"].startswith(name + "--")]
                attempt = next_attempt(name, previous, repair, recovery, task_calls)
                if attempt is None:
                    continue
                if (output / f"{name}--{attempt:02d}").exists():
                    raise RuntimeError(f"Unsettled attempt {name}; inspect live process/container before resuming")
                await trial(name, source, output, ledger, state, attempt)
                summarize(output, manifest, ledger, ledger.budget_exhausted, recovery_tasks)
                if ledger.budget_exhausted:
                    break
            summary = summarize(output, manifest, ledger, ledger.budget_exhausted, recovery_tasks)
            state.update(status="budget_stopped" if ledger.budget_exhausted else "finished", completed=summary["completed"], phase="terminal")
        except BaseException as exc:
            state.update(status="controller_error", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
            state["heartbeat"] = now()
            save(output / "state.json", state)
            ledger.db.close()


def validate_environment_recovery(output, manifest, ledger, recovery):
    calls = ledger.report()["calls"]
    eligible = set()
    for name in manifest["tasks"]:
        previous = sorted(output.glob(name + "--*/result.json"))
        if len(previous) != 1:
            continue
        record = read(previous[0])
        task_calls = [c for c in calls if c["task"].startswith(name + "--")]
        if (record.get("attempt") == 1 and record.get("status") == "setup_blocked"
                and record.get("cause") == "image_pull_failed" and record.get("reward") is None
                and not task_calls):
            eligible.add(name)
    authorized = set(recovery.get("retry_tasks", []))
    assert authorized == eligible, "Recovery task set must exactly match eligible setup-only failures"
    hashes = recovery.get("first_result_sha256", {})
    assert set(hashes) == authorized, "Recovery result hashes must cover exactly the authorized tasks"
    for name in authorized:
        assert digest(output / f"{name}--01" / "result.json") == hashes[name], name
    return authorized


def next_attempt(name, previous, repair, recovery=None, task_calls=()):
    if not previous:
        return 1
    if name in repair.get("retry_tasks", []) and len(previous) == 1 and read(previous[0]).get("cause") == "harness_budget_interrupted":
        return 2
    recovery = recovery or {}
    if name in recovery.get("retry_tasks", []):
        assert len(previous) == 1, f"Unexpected previous attempts for {name}"
        record = read(previous[0])
        assert record.get("attempt") == 1 and record.get("status") == "setup_blocked"
        assert record.get("cause") == "image_pull_failed" and record.get("reward") is None
        assert not task_calls, f"Recovery task {name} already has model calls"
        assert digest(previous[0]) == recovery["first_result_sha256"][name]
        return 2
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pilot", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
