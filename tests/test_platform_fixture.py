"""Disposable supervised processes for PF01; never targets an existing service."""
import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from harness.platform.config import PlatformConfig
from harness.platform.supervisor import Supervisor
from harness.storage import Store
from harness.core import utc_now


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if args.role == "supervisor":
        def command(role):
            return [sys.executable, str(Path(__file__).resolve()), role, "--data-dir", str(args.data_dir),
                    "--instance-id", args.instance_id, "--port", str(args.port)]
        Supervisor(PlatformConfig(data_dir=args.data_dir, port=args.port, startup_timeout=5), args.instance_id, command).run()
    elif args.role == "api":
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                content = json.dumps({"ready": True, "instance_id": args.instance_id}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *_): pass
        HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
    else:
        store = Store(args.data_dir / "app.db")
        while True:
            maintenance = store.get("system", "maintenance") or {}
            store.put("system", "worker_state", {"instance_id": args.instance_id,
                "status": "drained" if maintenance.get("enabled") else "ready", "active_run_ids": [], "heartbeat_at": utc_now()})
            time.sleep(.05)


if __name__ == "__main__":
    main()
