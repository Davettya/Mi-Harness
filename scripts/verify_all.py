"""Persist final verification logs across terminal/app interruptions."""

import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "docs/verification"
output.mkdir(parents=True, exist_ok=True)
commands = {
    "openapi": [sys.executable, "apps/web/scripts/export_openapi.py"],
    "api_types": [
        shutil.which("npm.cmd") or shutil.which("npm"),
        "--prefix",
        "apps/web",
        "run",
        "generate:api",
    ],
    "web_tests": [shutil.which("npm.cmd") or shutil.which("npm"), "--prefix", "apps/web", "test"],
    "web_build": [shutil.which("npm.cmd") or shutil.which("npm"), "--prefix", "apps/web", "run", "build"],
    "pytest": [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--disable-warnings",
        "--durations=10",
        "--junitxml=docs/verification/latest-pytest.xml",
    ],
    "compile": [sys.executable, "-m", "compileall", "-q", "harness"],
    "lint": [sys.executable, "-m", "ruff", "check", "harness", "--select", "E9,F821,F822,F823,F811"],
    "build": [sys.executable, "-m", "build"],
    "locked_runtime": [
        str(Path(sys.executable).with_name("uv.exe")) if sys.platform == "win32" else "uv",
        "export",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "--output-file",
        "docs/verification/runtime-requirements.txt",
    ],
    "wheel_install": [sys.executable, "scripts/verify_wheel.py"],
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
    if result.returncode and name in {"web_build", "build", "locked_runtime"}:
        # Never call an old build a newly verified release.
        break
report["completed_at"] = datetime.now(timezone.utc).isoformat()
(output / "verification-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
raise SystemExit(any(s["exit_code"] for s in report["steps"].values()))
