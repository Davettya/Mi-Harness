from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

from harness.core import HarnessError
from harness.platform.config import PlatformConfig, load_config
from harness.platform.cli import coordinated_backup, doctor
from harness.platform.supervisor import (start, status, stop, read_instance, matches_process,
    terminate_owned, process_identity, check_port, minimal_environment)
from harness.storage import Store
from harness.storage.backup import restore


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_windows_double_click_launcher_delegates_without_duplicate_configuration():
    root = Path(__file__).parents[1]
    launcher = (root / "start-mi-harness.cmd").read_text(encoding="utf-8")
    powershell = (root / "start-mi-harness.ps1").read_text(encoding="utf-8")

    assert 'start-mi-harness.ps1"' in launcher
    assert "-ExecutionPolicy Bypass" in launcher
    assert 'exit /b %EXIT_CODE%' in launcher
    assert "pause" in launcher
    assert "mi-harness serve" not in launcher
    assert "--port" not in launcher and "--data-dir" not in launcher
    assert "serve --open" in powershell
    assert powershell.isascii()


def test_configuration_precedence_narrowing_and_secret_rejection(tmp_path):
    user, workspace = tmp_path / "user.json", tmp_path / "workspace.json"
    user.write_text(json.dumps({"port": 8768, "data_dir": "relative", "permissions": {"network": False}}))
    workspace.write_text(json.dumps({"port": 8769, "permissions": {"network": True}}))
    config, explanation = load_config(user_file=user, workspace_file=workspace,
        environ={"HARNESS_PORT": "8770", "HARNESS_API_KEY": "canary-secret"}, cli={"port": 8771})
    assert config.port == 8771 and config.data_dir == (tmp_path / "relative").resolve()
    assert not config.permissions.network
    assert explanation["rejected_overrides"] and explanation["sources"]["port"] == "cli"
    assert "canary-secret" not in json.dumps(explanation)
    user.write_text('{"api_key":"canary-secret"}')
    with pytest.raises(HarnessError): load_config(user_file=user, environ={})
    with pytest.raises(ValueError): PlatformConfig(host="0.0.0.0")
    with pytest.raises(ValueError): PlatformConfig(credential_ref="plaintext-key")


def test_pf01_busy_port_and_identity_do_not_kill_other_process(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(HarnessError, match="监听端口"):
            check_port("127.0.0.1", sock.getsockname()[1])
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], creationflags=flags)
    try:
        identity = process_identity(process.pid, "not-our-instance", "api")
        assert not matches_process(identity, tmp_path)
        assert not terminate_owned(identity, tmp_path)
        assert process.poll() is None
        altered = dict(identity, create_time=identity["create_time"] - 100)
        assert not matches_process(altered, tmp_path)
    finally:
        process.terminate()
        process.wait(5)


def test_pf01_real_dual_process_start_duplicate_stop_and_redacted_doctor(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    port, instance_id = free_port(), str(uuid.uuid4())
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [sys.executable, str(Path(__file__).with_name("test_platform_fixture.py")), "supervisor",
               "--data-dir", str(data_dir), "--instance-id", instance_id, "--port", str(port)]
    with (tmp_path / "fixture.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=log, creationflags=flags, env=minimal_environment())
    try:
        limit = time.monotonic() + 12
        while time.monotonic() < limit:
            observed = status(data_dir)
            if observed["status"] == "ready": break
            if process.poll() is not None:
                pytest.fail((tmp_path / "fixture.log").read_text("utf-8"))
            time.sleep(.05)
        assert observed["status"] == "ready", observed
        assert observed["api_alive"] and observed["worker_alive"] and observed["supervisor_alive"]
        duplicate = start(PlatformConfig(data_dir=data_dir, port=port))
        assert duplicate["already_running"] and duplicate["instance_id"] == instance_id
        monkeypatch.setenv("CANARY_SECRET_TOKEN", "secret-not-for-children")
        assert "CANARY_SECRET_TOKEN" not in minimal_environment()
        diagnostic = doctor(data_dir)
        assert "secret-not-for-children" not in json.dumps(diagnostic)
        metadata = read_instance(data_dir)
        assert all(matches_process(identity, data_dir) for identity in metadata["processes"].values())
        stopped = stop(data_dir, timeout=10)
        assert stopped["status"] == "stopped", stopped
        process.wait(5)
        assert all(not matches_process(identity, data_dir) for identity in metadata["processes"].values())
    finally:
        if process.poll() is None:
            stop(data_dir, timeout=5)
            if process.poll() is None:
                metadata = read_instance(data_dir) or {}
                for identity in metadata.get("processes", {}).values():
                    terminate_owned(identity, data_dir)
                process.wait(5)


def test_d01_cli_coordinated_backup_restore_and_preserve_source(tmp_path):
    original, backup_dir, restored = tmp_path / "source", tmp_path / "backup", tmp_path / "restored"
    original.mkdir()
    store = Store(original / "app.db")
    store.put("fixture", "record", {"revision": 1, "content": "preserved"})
    bundle = original / "artifacts" / "skill-snapshots"
    bundle.mkdir(parents=True)
    (bundle / "sample").write_text("immutable skill snapshot")
    report = coordinated_backup(original, backup_dir)
    assert report["files"] >= 2 and not report["external_effects_rolled_back"]
    result = restore(backup_dir, restored)
    assert result["ok"]
    assert Store(restored / "app.db").get("fixture", "record")["content"] == "preserved"
    assert (restored / "artifacts" / "skill-snapshots" / "sample").read_text() == "immutable skill snapshot"
    assert store.get("fixture", "record")["content"] == "preserved"
    with pytest.raises(HarnessError): restore(backup_dir, original)


def test_pf01_windows_atomic_status_retry_keeps_last_good_snapshot(tmp_path, monkeypatch):
    from harness.platform import supervisor
    supervisor.write_instance(tmp_path, {"generation":1})
    original = Path.replace
    failures = []
    def sharing_violation(path, target):
        if len(failures)<2:
            failures.append(True)
            raise PermissionError("simulated Windows sharing violation")
        return original(path,target)
    monkeypatch.setattr(Path,"replace",sharing_violation)
    assert supervisor.write_instance(tmp_path,{"generation":2})
    assert supervisor.read_instance(tmp_path)=={"generation":2}
    def always_busy(*_):
        raise PermissionError("simulated persistent reader")
    monkeypatch.setattr(Path,"replace",always_busy)
    clock = iter([0,0,3])
    monkeypatch.setattr(supervisor.time,"monotonic",lambda:next(clock))
    assert supervisor.write_instance(tmp_path,{"generation":3}) is False
    assert supervisor.read_instance(tmp_path)=={"generation":2}
    assert not list(tmp_path.glob(".instance-*.tmp"))


def test_pf01_status_read_retries_sharing_violation_and_fails_closed(tmp_path, monkeypatch):
    from harness.platform import supervisor
    supervisor.write_instance(tmp_path, {"generation": 1})
    original = Path.read_text
    failures = []

    def transient_denial(path, *args, **kwargs):
        if path.name == "instance.json" and len(failures) < 2:
            failures.append(True)
            raise PermissionError("simulated sharing violation")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", transient_denial)
    assert supervisor.read_instance(tmp_path) == {"generation": 1}
    assert len(failures) == 2

    def persistent_denial(*args, **kwargs):
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(Path, "read_text", persistent_denial)
    clock = iter([0, 0, 3])
    monkeypatch.setattr(supervisor.time, "monotonic", lambda: next(clock))
    with pytest.raises(HarnessError) as error:
        supervisor.start(PlatformConfig(data_dir=tmp_path, port=8767))
    assert error.value.code == "INSTANCE_STATE_UNAVAILABLE"
    assert "private filesystem details" not in str(error.value)
