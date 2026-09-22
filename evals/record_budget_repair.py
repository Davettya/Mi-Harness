"""One-time evidence finalization for the documented stopped budget incident."""
import asyncio
import json
import shutil
from pathlib import Path

import psutil
import terminal_remaining as run


async def main():
    output = Path("docs/verification/terminal-remaining-20260920")
    state = run.read(output / "state.json")
    if psutil.pid_exists(state["pid"]):
        assert abs(psutil.Process(state["pid"]).create_time() - state["process_create_time"]) > 1, "Controller is still live"
    folder = output / "build-pov-ray--01"
    assert not (folder / "result.json").exists(), "Incident already recorded"
    container = "mi-remaining-build-pov-ray-1"
    code, inspected = await run.smoke.command("docker", "inspect", container)
    assert code == 0
    info = json.loads(inspected)[0]
    assert not info["State"]["Running"], "Original container still running"
    run.save(folder / "container-inspect-at-interruption.json", info)
    record = run.read(folder / "attempt-start.json")
    record.update(status="unscored", cause="harness_budget_interrupted", reward=None,
                  runtime_kind="interrupted", runtime_error_code="harness_budget_interrupted",
                  interruption_recorded_at=run.now(),
                  note="Controller stopped for documented output-limit budget defect; original evidence retained; authorized fresh attempt 02")
    record["evidence_sha256"] = {p.relative_to(folder).as_posix(): run.digest(p) for p in folder.rglob("*")
                                 if p.is_file() and p.suffix not in {".db", ".sqlite"} and not p.name.endswith(("-wal", "-shm"))}
    run.save(folder / "result.json", record)
    shutil.copy2(output / "state.json", output / "state-before-budget-repair.json")
    state.update(status="repair_interrupted", phase="controller_stopped", heartbeat=run.now())
    run.save(output / "state.json", state)
    ledger = run.smoke.Ledger(output / "budget.sqlite", budget_cny=25)
    ledger.reconcile_legacy_unknown()
    summary = run.summarize(output, run.read(output / "manifest.json"), ledger)
    print(json.dumps({"budget": summary["budget"], "interrupted": record["task"], "hash_entries": len(record["evidence_sha256"])}))
    ledger.db.close()


if __name__ == "__main__":
    asyncio.run(main())
