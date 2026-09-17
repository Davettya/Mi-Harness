"""Persist final verification logs across terminal/app interruptions."""

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "docs/verification"
output.mkdir(parents=True, exist_ok=True)
commands = {
    "pytest": [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--junitxml=docs/verification/latest-pytest.xml",
    ],
    "compile": [sys.executable, "-m", "compileall", "-q", "harness"],
    "lint": [sys.executable, "-m", "ruff", "check", "harness", "--select", "E9,F821,F822,F823,F811"],
    "build": [sys.executable, "-m", "build"],
}
report = {"started_at": datetime.now(timezone.utc).isoformat(), "steps": {}}
for name, command in commands.items():
    begin = time.monotonic()
    with (output / f"{name}.log").open("wb") as log:
        result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT)
    report["steps"][name] = {
        "exit_code": result.returncode,
        "seconds": round(time.monotonic() - begin, 3),
        "command": command[1:],
    }
    (output / "verification-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(name, result.returncode, flush=True)
report["completed_at"] = datetime.now(timezone.utc).isoformat()
(output / "verification-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
raise SystemExit(any(s["exit_code"] for s in report["steps"].values()))
