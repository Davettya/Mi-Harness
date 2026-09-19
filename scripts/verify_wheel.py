"""Install the wheel and locked dependencies outside this checkout, then run it."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

import httpx

root = Path(__file__).resolve().parents[1]
output = root / "docs/verification"
project = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]
wheel = root / "dist" / f"{project['name'].replace('-', '_')}-{project['version']}-py3-none-any.whl"
uv = root / ".venv/Scripts/uv.exe"
flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
report = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "wheel": wheel.name,
    "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    "checks": {},
}
with ZipFile(wheel) as package:
    names = set(package.namelist())
    assert "harness/resources/web/index.html" in names
    assert "harness/resources/skills/file-audit/SKILL.md" in names
    report["checks"]["bundled_web_and_skill"] = True
taskdir = Path(tempfile.mkdtemp(prefix="harness-wheel-verification-"))
environment = taskdir / "venv"
python = environment / "Scripts/python.exe"


def run(args):
    result = subprocess.run(
        [str(a) for a in args],
        cwd=taskdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result.stdout


try:
    run([uv, "venv", environment, "--python", sys.executable])
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            python,
            "--require-hashes",
            "-r",
            root / "docs/verification/runtime-requirements.txt",
        ]
    )
    run([uv, "pip", "install", "--python", python, "--no-deps", wheel])
    imported = run([python, "-c", "import harness;print(harness.__file__)"]).strip()
    assert str(environment).lower() in imported.lower() and "site-packages" in imported
    report["checks"]["isolated_install"] = True
    workspace = taskdir / "workspace"
    workspace.mkdir()
    data = taskdir / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    cli = [python, "-m", "harness.platform.cli", "--data-dir", data]
    try:
        launched = json.loads(run([*cli, "serve", "--port", port, "--startup-timeout", 60]))
        assert launched["status"] == "ready", launched
        report["checks"]["installed_supervisor_api_worker"] = True
        with httpx.Client(base_url=base, trust_env=False, timeout=15) as client:
            page = client.get("/")
            assert page.status_code == 200 and "<html" in page.text
            assert (
                client.post(
                    "/api/auth/local", headers={"Origin": base}
                ).status_code
                == 204
            )
            client.headers.update({"Origin": base, "X-CSRF-Token": client.cookies["harness_csrf"]})
            import uuid

            def post(path, body):
                response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
                assert response.is_success, (response.status_code, response.text)
                return response.json()

            w = post("/api/workspaces", {"name": "Wheel verification", "root_candidate": str(workspace)})
            session = post("/api/sessions", {"workspace_id": w["id"], "agent_spec_id": "default"})
            receipt = post(
                f"/api/sessions/{session['id']}/runs",
                {
                    "branch_id": session["default_branch_id"],
                    "expected_branch_revision": 1,
                    "agent_spec_revision": 1,
                    "content_parts": [{"type": "text", "text": "安装包运行验证"}],
                },
            )
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                result = client.get("/api/runs/" + receipt["run_id"]).json()
                if result["status"] in {"completed", "failed", "needs_review"}:
                    break
                time.sleep(0.1)
            assert result["status"] == "completed", result
            assert (data / "checkpoints.db").exists()
            report["checks"]["installed_graph_run_completed"] = True
    finally:
        stopped = json.loads(run([*cli, "stop", "--timeout", 15]))
        assert stopped["status"] == "stopped", stopped
        report["checks"]["installed_stop"] = True
    report["passed"] = True
except Exception as error:
    report["passed"] = False
    report["error"] = str(error)
    raise
finally:
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    (output / "wheel-verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
