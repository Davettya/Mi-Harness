from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import uuid
import urllib.request
from pathlib import Path

import portalocker
import psutil

from harness.core import HarnessError, canonical_json, utc_now
from harness.storage import Store
from .config import PlatformConfig


def minimal_environment():
    allowed = ("SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
               "USERPROFILE", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result["PYTHONUNBUFFERED"] = "1"
    result["PYTHONIOENCODING"] = "utf-8"
    return result


def read_instance(data_dir: Path):
    deadline = None
    while True:
        try:
            return json.loads((Path(data_dir) / "instance.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        except PermissionError:
            # Windows can briefly deny reads while an atomic snapshot is replaced.
            # Persistent denial must not be mistaken for a stopped instance.
            if deadline is None:
                deadline = time.monotonic() + 2
            if time.monotonic() >= deadline:
                raise HarnessError("INSTANCE_STATE_UNAVAILABLE", "暂时无法读取实例状态，请稍后重试", 503) from None
            time.sleep(.02)


def write_instance(data_dir: Path, value):
    target = Path(data_dir) / "instance.json"
    temporary = target.with_name(".instance-" + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(canonical_json(value))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                temporary.replace(target)
                return True
            except PermissionError:
                # Windows readers and virus scanners can briefly hold no-share-delete handles.
                # Last atomic snapshot remains authoritative until a later loop iteration.
                if time.monotonic() >= deadline:
                    return False
                time.sleep(.02)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError:
            pass


def process_identity(pid: int, instance_id: str, role: str):
    process = psutil.Process(pid)
    return {"pid": pid, "create_time": process.create_time(), "instance_id": instance_id, "role": role}


def matches_process(identity, data_dir: Path):
    if not identity or not isinstance(identity.get("pid"), int):
        return False
    try:
        process = psutil.Process(identity["pid"])
        if abs(process.create_time() - identity["create_time"]) > .01 or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        args = process.cmdline()
        if "--instance-id" not in args or args[args.index("--instance-id") + 1] != identity["instance_id"]:
            return False
        if "--data-dir" not in args:
            return False
        actual = Path(args[args.index("--data-dir") + 1]).resolve()
        return os.path.normcase(str(actual)) == os.path.normcase(str(Path(data_dir).resolve()))
    except (psutil.Error, KeyError, ValueError, IndexError, OSError):
        return False


def terminate_owned(identity, data_dir: Path, timeout=5):
    if not matches_process(identity, data_dir):
        return False
    process = psutil.Process(identity["pid"])
    process.terminate()
    try:
        process.wait(timeout)
    except psutil.TimeoutExpired:
        if not matches_process(identity, data_dir):
            return False
        process.kill()
        process.wait(timeout)
    return True


def check_port(host: str, port: int):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family) as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise HarnessError("PORT_IN_USE", "监听端口已被占用；没有停止占用该端口的进程", 409) from exc


def health(config, instance_id):
    host = "[::1]" if config.host == "::1" else config.host
    try:
        # Disable ambient proxy routing for the loopback identity probe.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{config.port}/health/ready", timeout=.5) as response:
            value = json.loads(response.read(4096))
            return value if value.get("instance_id") == instance_id else None
    except Exception:
        return None


def status(data_dir: Path):
    data_dir = Path(data_dir).resolve()
    instance = read_instance(data_dir)
    if not instance:
        return {"status": "stopped", "api_alive": False, "worker_alive": False, "supervisor_alive": False}
    alive = {role + "_alive": matches_process(instance.get("processes", {}).get(role), data_dir)
             for role in ("supervisor", "api", "worker")}
    report = {"instance_id": instance["instance_id"], "host": instance["host"], "port": instance["port"], **alive,
              "status": instance.get("status", "unknown"), "diagnostics": instance.get("diagnostics", [])}
    if not any(alive.values()):
        report["status"] = "stopped"
    elif not all(alive.values()):
        report["status"] = "degraded"
    if (data_dir / "app.db").exists():
        try:
            store = Store(data_dir / "app.db")
            report["database"] = store.integrity()
            report["worker_state"] = store.get("system", "worker_state")
            report["maintenance"] = store.get("system", "maintenance")
        except Exception as exc:
            report["database"] = {"ok": False, "error_code": type(exc).__name__}
    return report


def _spawn(command, log_path: Path):
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with log_path.open("ab", buffering=0) as log:
        return subprocess.Popen(command, cwd=Path(__file__).resolve().parents[2], env=minimal_environment(),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=creationflags, close_fds=True)


def start(config: PlatformConfig):
    data_dir = config.data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    previous = status(data_dir)
    if any(previous.get(role + "_alive") for role in ("supervisor", "api", "worker")):
        return {**previous, "already_running": True}
    check_port(config.host, config.port)
    instance_id = str(uuid.uuid4())
    # Unique launch files prevent simultaneous launches from substituting one another's settings.
    launch_path = data_dir / (".launch-" + instance_id + ".json")
    launch_path.write_text(config.model_copy(update={"data_dir": data_dir, "instance_id": instance_id}).model_dump_json(), encoding="utf-8")
    command = [sys.executable, "-m", "harness.platform.supervisor", "--data-dir", str(data_dir),
        "--instance-id", instance_id, "--host", config.host, "--port", str(config.port),
        "--drain-timeout", str(config.drain_timeout), "--effective-config", str(launch_path)]
    process = _spawn(command, data_dir / "supervisor.log")
    deadline = time.monotonic() + config.startup_timeout
    while time.monotonic() < deadline:
        current = read_instance(data_dir)
        if current and current.get("instance_id") == instance_id and current.get("status") in ("ready", "failed", "degraded"):
            return status(data_dir)
        if process.poll() is not None:
            # A simultaneous launch may have acquired the directory lock first.
            other = status(data_dir)
            if other.get("supervisor_alive"):
                return {**other, "already_running": True}
            raise HarnessError("SUPERVISOR_START_FAILED", "监督进程启动失败；请运行 doctor 查看脱敏状态", 503)
        time.sleep(.05)
    return {**status(data_dir), "status": "starting", "diagnostics": ["startup readiness deadline exceeded"]}


def stop(data_dir: Path, timeout: float = 30):
    data_dir = Path(data_dir).resolve()
    before = status(data_dir)
    if before["status"] == "stopped":
        return before
    instance_id = before["instance_id"]
    store = Store(data_dir / "app.db")
    store.put("system", "maintenance", {"enabled": True, "instance_id": instance_id,
        "request_id": str(uuid.uuid4()), "action": "stop", "status": "requested", "requested_at": utc_now()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = status(data_dir)
        if current["status"] == "stopped":
            return current
        # Supervisor may have crashed. Recover shutdown only after worker acknowledges drain.
        if not current["supervisor_alive"]:
            state = current.get("worker_state") or {}
            if not current["worker_alive"] or (state.get("instance_id") == instance_id and state.get("status") == "drained" and not state.get("active_run_ids")):
                metadata = read_instance(data_dir)
                for role in ("worker", "api"):
                    terminate_owned(metadata["processes"].get(role), data_dir)
                return status(data_dir)
        time.sleep(.1)
    return {**status(data_dir), "stop_complete": False,
        "diagnostics": ["排空未完成；维护模式保持启用，现有任务与进程保留。再次 stop 或查看任务状态。"]}


class Supervisor:
    def __init__(self, config: PlatformConfig, instance_id: str, command_factory=None):
        self.config, self.instance_id = config, instance_id
        self.data_dir = config.data_dir.resolve()
        self.command_factory = command_factory or self.default_command
        self.record = {"instance_id": instance_id, "host": config.host, "port": config.port,
                       "status": "starting", "processes": {}, "diagnostics": [], "started_at": utc_now()}

    def default_command(self, role):
        module = "harness.server" if role == "api" else "harness.scheduler.worker"
        command = [sys.executable, "-m", module, "--data-dir", str(self.data_dir), "--instance-id", self.instance_id]
        if role == "api":
            command.extend(["--host", self.config.host, "--port", str(self.config.port)])
        return command

    def run(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(str(self.data_dir / "instance.lock"), timeout=0)
        lock.acquire()
        try:
            previous = status(self.data_dir)
            if any(previous.get(role + "_alive") for role in ("api", "worker")):
                raise HarnessError("ORPHAN_INSTANCE", "已有本实例子进程存活，请先安全停止", 409)
            check_port(self.config.host, self.config.port)
            store = Store(self.data_dir / "app.db")
            effective = self.config.model_copy(update={"data_dir": self.data_dir, "instance_id": self.instance_id})
            store.put("system", "effective_config", {"instance_id": self.instance_id,
                "settings": effective.model_dump(mode="json"), "published_at": utc_now()})
            store.put("system", "maintenance", {"enabled": False, "instance_id": self.instance_id, "status": "running"})
            self.record["processes"]["supervisor"] = process_identity(os.getpid(), self.instance_id, "supervisor")
            write_instance(self.data_dir, self.record)
            children = {}
            try:
                for role in ("api", "worker"):
                    children[role] = _spawn(self.command_factory(role), self.data_dir / (role + ".log"))
                    self.record["processes"][role] = process_identity(children[role].pid, self.instance_id, role)
                # Startup evidence is process liveness plus the worker's explicit ready heartbeat.
                started = time.monotonic()
                draining_since = None
                while True:
                    alive = {role: child.poll() is None for role, child in children.items()}
                    state = store.get("system", "worker_state") or {}
                    maintenance = store.get("system", "maintenance") or {}
                    if maintenance.get("enabled") and maintenance.get("instance_id") == self.instance_id and maintenance.get("action") == "stop":
                        self.record["status"] = "draining"
                        draining_since = draining_since or time.monotonic()
                        if not alive["worker"] or (state.get("instance_id") == self.instance_id and state.get("status") == "drained" and not state.get("active_run_ids")):
                            for role in ("worker", "api"):
                                terminate_owned(self.record["processes"][role], self.data_dir)
                            self.record["status"] = "stopped"
                            write_instance(self.data_dir, self.record)
                            return
                        if time.monotonic() - draining_since > self.config.drain_timeout:
                            self.record["diagnostics"] = ["worker drain deadline exceeded; no active process was forcibly stopped"]
                    elif not all(alive.values()):
                        self.record["status"] = "degraded"
                        self.record["diagnostics"] = [role + " process exited" for role, running in alive.items() if not running]
                        if not any(alive.values()):
                            self.record["status"] = "failed"
                            write_instance(self.data_dir, self.record)
                            return
                    elif state.get("instance_id") == self.instance_id and state.get("status") in ("ready", "running", "drained"):
                        observed = health(self.config, self.instance_id)
                        if observed and observed.get("ready"):
                            self.record["status"] = "ready"
                        elif time.monotonic() - started > self.config.startup_timeout:
                            self.record["status"] = "degraded"
                            self.record["diagnostics"] = ["API readiness or instance identity could not be verified"]
                    elif time.monotonic() - started > self.config.startup_timeout:
                        self.record["status"] = "degraded"
                        self.record["diagnostics"] = ["worker has not published a ready heartbeat"]
                    write_instance(self.data_dir, self.record)
                    time.sleep(.1)
            except BaseException:
                # Only children created by this launch, with unchanged identities, are reclaimed.
                for identity in self.record["processes"].values():
                    if identity["role"] != "supervisor":
                        terminate_owned(identity, self.data_dir)
                self.record["status"] = "failed"
                self.record["diagnostics"] = ["supervisor startup or lifecycle exception"]
                write_instance(self.data_dir, self.record)
                raise
        finally:
            lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--drain-timeout", type=float, default=30)
    parser.add_argument("--effective-config", type=Path)
    args = parser.parse_args(argv)
    if args.effective_config:
        expected = args.data_dir.resolve() / (".launch-" + args.instance_id + ".json")
        if args.effective_config.resolve() != expected:
            raise HarnessError("CONFIG_INSTANCE_MISMATCH", "启动配置路径不属于当前实例", 409)
        config = PlatformConfig.model_validate_json(expected.read_text("utf-8"))
        if config.data_dir.resolve() != args.data_dir.resolve() or config.instance_id != args.instance_id:
            raise HarnessError("CONFIG_INSTANCE_MISMATCH", "启动配置身份不匹配", 409)
    else:
        config = PlatformConfig(data_dir=args.data_dir, host=args.host, port=args.port, drain_timeout=args.drain_timeout)
    Supervisor(config, args.instance_id).run()


if __name__ == "__main__":
    main()
