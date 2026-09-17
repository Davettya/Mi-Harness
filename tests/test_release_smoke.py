"""Production supervisor/API/worker/backup path, all in a disposable workspace."""

import socket
import time
import uuid
from pathlib import Path

import httpx
from harness.platform.config import PlatformConfig
from harness.platform.supervisor import start, stop
from harness.platform.cli import coordinated_backup
from harness.storage.backup import restore
from harness.server.auth import AuthService
from harness.storage import Store


def test_production_launch_run_artifact_backup_restore(tmp_path):
    data, workspace = tmp_path / "data", tmp_path / "workspace"
    workspace.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = PlatformConfig(data_dir=data, port=port, startup_timeout=35)
    try:
        launched = start(config)
        assert launched["status"] == "ready", launched
        assert start(config)["already_running"]
        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
            assert client.get("/").status_code == 200
            assert client.get("/api/workspaces").status_code == 401
            ticket = AuthService(Store(data / "app.db")).issue_ticket()
            assert (
                client.post(
                    "/api/auth/exchange", json={"ticket": ticket}, headers={"Origin": base}
                ).status_code
                == 204
            )
            client.headers.update({"Origin": base, "X-CSRF-Token": client.cookies["harness_csrf"]})

            def post(path, body):
                response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid.uuid4())})
                assert response.is_success, (response.status_code, response.text)
                return response.json()

            w = post("/api/workspaces", {"name": "发布冒烟", "root_candidate": str(workspace)})
            session = post("/api/sessions", {"workspace_id": w["id"], "agent_spec_id": "default"})
            receipt = post(
                f"/api/sessions/{session['id']}/runs",
                {
                    "branch_id": session["default_branch_id"],
                    "expected_branch_revision": 1,
                    "agent_spec_revision": 1,
                    "content_parts": [
                        {
                            "type": "text",
                            "text": '/tool artifact_create {"content":"发布证据","mime_type":"text/plain","name":"proof.txt"}',
                        }
                    ],
                },
            )
            run_id = receipt["run_id"]
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                run = client.get(f"/api/runs/{run_id}").json()
                if run["status"] in {"completed", "failed", "needs_review"}:
                    break
                time.sleep(0.1)
            assert run["status"] == "completed", run
            assert run["artifacts"], run
            artifact = run["artifacts"][0]
            assert client.get(f"/api/artifacts/{artifact['artifact_id']}").text == "发布证据"
            metrics = client.get("/api/diagnostics/metrics")
            assert metrics.status_code == 200 and metrics.json()["model_attempts"] >= 2
        backup_dir = tmp_path / "backup"
        report = coordinated_backup(data, backup_dir, timeout=15)
        assert report["files"] >= 3
        assert (backup_dir / "checkpoints.db").exists()
        restored = tmp_path / "restored"
        assert restore(backup_dir, restored)["ok"]
        assert Store(restored / "app.db").run(run_id)["status"] == "completed"
        assert (workspace).is_dir()
        assert start(PlatformConfig(data_dir=restored, port=port, startup_timeout=35))["status"] == "ready"
        try:
            with httpx.Client(base_url=base, trust_env=False, timeout=10) as client:
                ticket = AuthService(Store(restored / "app.db")).issue_ticket()
                assert (
                    client.post(
                        "/api/auth/exchange", json={"ticket": ticket}, headers={"Origin": base}
                    ).status_code
                    == 204
                )
                client.headers.update({"Origin": base, "X-CSRF-Token": client.cookies["harness_csrf"]})
                assert client.get(f"/api/artifacts/{artifact['artifact_id']}").text == "发布证据"
                branch = Store(restored / "app.db").branch(session["default_branch_id"])
                resumed = post(
                    f"/api/sessions/{session['id']}/runs",
                    {
                        "branch_id": branch["id"],
                        "expected_branch_revision": branch["revision"],
                        "agent_spec_revision": 1,
                        "content_parts": [{"type": "text", "text": "恢复后继续"}],
                    },
                )
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    resumed_state = client.get(f"/api/runs/{resumed['run_id']}").json()
                    if resumed_state["status"] in {"completed", "failed", "needs_review"}:
                        break
                    time.sleep(0.1)
                assert resumed_state["status"] == "completed", resumed_state
        finally:
            stop(restored, timeout=10)
    finally:
        stop(data, timeout=10)
