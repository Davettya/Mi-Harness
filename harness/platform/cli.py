from __future__ import annotations

import argparse
import getpass
import importlib.metadata
import json
import os
import platform
import sys
import webbrowser
from pathlib import Path

import portalocker

from harness.core import HarnessError
from harness.storage import Store
from harness.storage.backup import backup, restore
from .config import default_data_dir, load_config
from .credentials import CredentialVault
from .supervisor import start, status, stop


def doctor(data_dir: Path):
    report = {"platform": platform.system(), "platform_release": platform.release(),
        "python": platform.python_version(), "service": status(data_dir), "dependencies": {},
        "credential_backend": None, "secret_values_included": False}
    for package in ("mcp", "langchain", "langgraph", "fastapi", "uvicorn", "pydantic"):
        try:
            report["dependencies"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            report["dependencies"][package] = "not-installed"
    try:
        import keyring
        backend = keyring.get_keyring()
        report["credential_backend"] = type(backend).__module__ + "." + type(backend).__name__
    except Exception as exc:
        report["credential_backend"] = type(exc).__name__
    # No raw logs, environment, client config, task text or credentials are exported.
    if (Path(data_dir) / "app.db").exists():
        record = Store(Path(data_dir) / "app.db").get("system", "effective_config")
        if record:
            report["running_config"] = record
    return CredentialVault().redact(report)


def coordinated_backup(data_dir: Path, destination: Path, *, timeout=30):
    data_dir, destination = Path(data_dir).resolve(), Path(destination).resolve()
    if destination == data_dir or destination.is_relative_to(data_dir):
        raise HarnessError("BACKUP_LOCATION", "备份目标必须位于数据目录之外", 422)
    before = status(data_dir)
    if before["status"] != "stopped":
        after = stop(data_dir, timeout)
        if any(after.get(role + "_alive") for role in ("supervisor", "api", "worker")):
            raise HarnessError("MAINTENANCE_REQUIRED", "写入者未完全排空，备份没有执行", 409)
    data_dir.mkdir(parents=True, exist_ok=True)
    # Excludes a concurrent service start while all writers and GC are stopped.
    with portalocker.Lock(str(data_dir / "instance.lock"), timeout=0):
        current = status(data_dir)
        if any(current.get(role + "_alive") for role in ("api", "worker", "supervisor")):
            raise HarnessError("MAINTENANCE_REQUIRED", "备份期间服务已重新启动", 409)
        manifest = backup(data_dir, destination, writers_stopped=True)
    return {"destination": str(destination), "files": len(manifest["files"]),
            "schema_version": manifest["schema_version"], "service_restarted": False,
            "external_effects_rolled_back": False}


def parser():
    command = argparse.ArgumentParser(prog="mi-harness", description="Mi Harness 本地服务管理")
    command.add_argument("--data-dir", type=Path)
    command.add_argument("--config", type=Path)
    sub = command.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int)
    serve.add_argument("--startup-timeout", type=float)
    serve.add_argument("--drain-timeout", type=float)
    serve.add_argument("--open", action="store_true", help="启动后自动连接浏览器工作台")
    sub.add_parser("status")
    stop_parser = sub.add_parser("stop")
    stop_parser.add_argument("--timeout", type=float, default=30)
    sub.add_parser("doctor")
    sub.add_parser("pair", help="生成短期一次性配对口令，仅打印到当前终端")
    backup_parser = sub.add_parser("backup")
    backup_parser.add_argument("destination", type=Path)
    backup_parser.add_argument("--timeout", type=float, default=30)
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("source", type=Path)
    restore_parser.add_argument("destination", type=Path, help="必须是新的空目录；现有数据保持不变")
    credential = sub.add_parser("credential-set")
    credential.add_argument("--target", required=True, help="模型/MCP服务绑定目标，不能跨服务复用")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    overrides = {key: getattr(args, key, None) for key in
                 ("data_dir", "host", "port", "startup_timeout", "drain_timeout")}
    try:
        config, explanation = load_config(user_file=args.config, cli=overrides)
        data_dir = config.data_dir.resolve()
        if args.command == "serve":
            result = start(config)
            if args.open:
                if result.get("status") != "ready":
                    raise HarnessError("SERVICE_NOT_READY", "服务尚未就绪，请查看 status 后重试", 503)
                from harness.server.auth import AuthService
                host = result.get("host", config.host)
                port = result.get("port", config.port)
                authority = f"[{host}]" if ":" in host else host
                ticket = AuthService(Store(data_dir / "app.db")).issue_ticket(ttl_seconds=60)
                # Fragment is never sent in an HTTP request or printed in service output.
                opened = webbrowser.open(f"http://{authority}:{port}/#launch={ticket}")
                result = {**result, "browser_opened": opened}
                if not opened:
                    raise HarnessError("BROWSER_OPEN_FAILED", "浏览器未能自动打开，请重试或使用 mi-harness pair 备用连接", 503)
        elif args.command == "status":
            result = status(data_dir)
        elif args.command == "stop":
            result = stop(data_dir, args.timeout)
        elif args.command == "doctor":
            result = doctor(data_dir)
            result["config"] = explanation
        elif args.command == "pair":
            from harness.server.auth import AuthService
            data_dir.mkdir(parents=True, exist_ok=True)
            ticket = AuthService(Store(data_dir / "app.db")).issue_ticket(ttl_seconds=300)
            print(ticket)
            return 0
        elif args.command == "credential-set":
            reference = CredentialVault().put(args.target, getpass.getpass("凭据（输入不回显）: "))
            result = {"credential_ref": reference, "target": args.target}
        elif args.command == "backup":
            result = coordinated_backup(data_dir, args.destination, timeout=args.timeout)
        else:
            source, target = args.source.resolve(), args.destination.resolve()
            if source == target or target.is_relative_to(source):
                raise HarnessError("RESTORE_LOCATION", "恢复目标必须是备份目录之外的新空目录", 422)
            result = restore(source, target)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if args.command == "stop" and result.get("stop_complete") is False:
            return 2
        return 0
    except HarnessError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": "操作失败，未输出敏感异常内容"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
