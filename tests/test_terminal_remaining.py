import importlib.util
import json
import sys
from pathlib import Path

import pytest


def module():
    root = Path(__file__).parents[1] / "evals"
    for name in ("terminal_smoke", "terminal_remaining"):
        spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
        item = importlib.util.module_from_spec(spec)
        sys.modules[name] = item
        spec.loader.exec_module(item)
    return sys.modules["terminal_remaining"]


def test_25_cny_budget_is_durable_includes_unknown_and_cannot_be_raised(tmp_path):
    mod = module()
    path = tmp_path / "budget.sqlite"
    ledger = mod.smoke.Ledger(path, budget_cny=25)
    body = {"model": "deepseek-flash", "reasoning_effort": "high", "thinking": {"type": "enabled"}, "max_tokens": 2_000_000}
    key = ledger.reserve("first--01", body)
    assert 16_000_000 < ledger.spent() < 17_000_000
    ledger.settle(key, None)
    with pytest.raises(RuntimeError, match="BUDGET_EXHAUSTED"):
        ledger.reserve("second--01", body)
    assert ledger.budget_exhausted
    reserved = ledger.spent()
    ledger.db.close()
    reopened = mod.smoke.Ledger(path, budget_cny=25)
    assert reopened.spent() == reserved
    assert reopened.report()["budget_cny"] == 25
    assert reopened.report()["unknown_calls"] == 1
    reopened.db.close()
    with pytest.raises(RuntimeError, match="Cannot change"):
        mod.smoke.Ledger(path, budget_cny=30)


def test_failures_and_null_reward_are_not_hidden_or_promoted(tmp_path):
    mod = module()
    assert mod.classify({"kind": "final"}, 0, 0)["status"] == "failed"
    assert mod.classify({"kind": "final"}, None, 124)["cause"] == "verifier_timeout"
    limited = mod.classify({"error": {"code": "deadline_exceeded"}}, 0, 0)
    assert limited["cause"] == "agent_limit" and limited["status"] == "failed"
    uncertain = mod.classify({"error": {"code": "provider_unavailable"}}, None, 1)
    assert uncertain["status"] == "unscored" and uncertain["runtime_error_code"] == "provider_unavailable"
    folder = tmp_path / "a--01"
    folder.mkdir()
    mod.save(folder / "result.json", {"task": "a", "status": "failed", "reward": 0})
    ledger = mod.smoke.Ledger(tmp_path / "budget.sqlite", budget_cny=25)
    summary = mod.summarize(tmp_path, {"tasks": ["a", "b"]}, ledger, budget_stopped=True)
    assert summary["counts"] == {"failed": 1, "not_attempted_budget": 1}
    assert summary["rows"][1]["reward"] is None
    assert json.loads((folder / "result.json").read_text())["reward"] == 0
    ledger.db.close()


async def test_logged_timeout_preserves_partial_output_and_reaps_process(tmp_path):
    mod = module()
    path = tmp_path / "timeout.log"
    result = await mod.logged([sys.executable, "-c", "import time; print('before timeout',flush=True); time.sleep(20)"], path, .8)
    assert result["exit_code"] == 124
    assert "before timeout" in path.read_text()


def test_wire_limit_guard_and_legacy_unknown_repair(tmp_path):
    mod = module()
    ledger = mod.smoke.Ledger(tmp_path / "ledger.db", budget_cny=25)
    body = {"model": "deepseek-flash", "reasoning_effort": "high", "thinking": {"type": "enabled"}}
    for invalid in (body, {**body, "max_completion_tokens": 16384}, {**body, "max_tokens": 0}):
        with pytest.raises(RuntimeError, match="explicit positive max_tokens"):
            ledger.reserve("t", invalid)
    ledger.db.execute("INSERT INTO calls VALUES (?,?,?,?,?)", ("legacy", "t--01", 180000, None, json.dumps(body)))
    ledger.db.commit()
    ledger.reconcile_legacy_unknown()
    expected = 180000 + (393216 - 16384) * 8
    assert ledger.spent() == expected
    ledger.reconcile_legacy_unknown()
    assert ledger.spent() == expected
    ledger.reserve("new", {**body, "max_tokens": 16384})
    before = ledger.spent()
    ledger.reconcile_legacy_unknown()
    assert ledger.spent() == before
    ledger.db.close()


def test_retry_requires_named_harness_interruption(tmp_path):
    mod = module()
    result = tmp_path / "result.json"
    repair = {"retry_tasks": ["build-pov-ray"]}
    assert mod.next_attempt("new", [], repair) == 1
    for cause in ("task_failure", "image_pull_failed", "runtime_error_needs_attribution"):
        mod.save(result, {"cause": cause})
        assert mod.next_attempt("build-pov-ray", [result], repair) is None
    mod.save(result, {"cause": "harness_budget_interrupted"})
    assert mod.next_attempt("build-pov-ray", [result], {}) is None
    assert mod.next_attempt("build-pov-ray", [result], repair) == 2
    assert mod.next_attempt("build-pov-ray", [result, result], repair) is None


def test_environment_recovery_requires_exact_unstarted_setup_failures(tmp_path):
    mod = module()
    folder = tmp_path / "infra--01"
    folder.mkdir()
    result = folder / "result.json"
    mod.save(result, {"task": "infra", "attempt": 1, "status": "setup_blocked",
                      "cause": "image_pull_failed", "reward": None})
    recovery = {"retry_tasks": ["infra"], "first_result_sha256": {"infra": mod.digest(result)}}
    ledger = mod.smoke.Ledger(tmp_path / "budget.sqlite", budget_cny=25)
    assert mod.validate_environment_recovery(tmp_path, {"tasks": ["infra"]}, ledger, recovery) == {"infra"}
    assert mod.next_attempt("infra", [result], {}, recovery, []) == 2
    with pytest.raises(AssertionError, match="already has model calls"):
        mod.next_attempt("infra", [result], {}, recovery, [{"task": "infra--01"}])
    mod.save(result, {"task": "infra", "attempt": 1, "status": "failed",
                      "cause": "task_failure", "reward": 0})
    with pytest.raises(AssertionError):
        mod.next_attempt("infra", [result], {}, recovery, [])
    ledger.db.close()


def test_budget_stop_marks_unstarted_recovery_tasks_as_censored(tmp_path):
    mod = module()
    folder = tmp_path / "infra--01"
    folder.mkdir()
    mod.save(folder / "result.json", {"task": "infra", "attempt": 1, "status": "setup_blocked",
                                      "cause": "image_pull_failed", "reward": None})
    ledger = mod.smoke.Ledger(tmp_path / "budget.sqlite", budget_cny=25)
    summary = mod.summarize(tmp_path, {"tasks": ["infra"]}, ledger, budget_stopped=True,
                            recovery_tasks={"infra"})
    assert summary["rows"][0]["status"] == "not_attempted_budget"
    assert summary["rows"][0]["cause"] == "recovery_budget_exhausted"
    assert json.loads((folder / "result.json").read_text())["status"] == "setup_blocked"
    ledger.db.close()
