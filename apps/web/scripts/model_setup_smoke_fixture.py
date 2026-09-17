"""Isolated browser UI fixture; never connects to providers or the user's Harness data.

Run after npm build. All state and fabricated credentials are transient in memory.
The evidence endpoint records only action names, model names and key presence.
"""
from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8879)
    args = parser.parse_args()
    dist = Path(__file__).resolve().parents[1] / "dist"
    profiles = [
        {"id": "demo", "revision": 1, "provider_id": "demo", "adapter_id": "demo", "model_id": "deterministic-demo"},
        {"id": "fixture-existing", "revision": 2, "provider_id": "openai", "adapter_id": "openai", "model_id": "gpt-4.1-mini", "credential_ref": "fixture-reference-only"},
    ]
    state = {"active": "fixture-existing@2", "evidence": []}
    catalog = [
        {"id": provider, "name": name, "requires_api_key": provider not in {"ollama", "custom"}, "default_base_url": "", "allow_custom_base_url": provider == "custom", "models": [{"id": model, "name": model}] if model else [], "documentation_url": ""}
        for provider, name, model in [
            ("openai", "OpenAI", "gpt-4.1-mini"), ("anthropic", "Anthropic", "claude-sonnet-4-5"),
            ("deepseek", "DeepSeek", "deepseek-chat"), ("qwen", "通义千问", "qwen-plus"),
            ("tencent", "腾讯混元", "hunyuan-turbos-latest"), ("siliconflow", "硅基流动", "deepseek-ai/DeepSeek-V3"),
            ("ollama", "Ollama", "qwen3:8b"), ("custom", "自定义兼容服务", ""),
        ]
    ]

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(dist), **kwargs)

        def log_message(self, *_args):
            pass

        def reply(self, data, status=200):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Set-Cookie", "harness_csrf=fixture-csrf; Path=/; SameSite=Strict")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/api/auth/session":
                return self.reply({"owner_id": "fixture", "csrf_token": "fixture-csrf", "capabilities": {}})
            if path == "/api/model-providers":
                return self.reply({"items": catalog})
            if path in {"/api/agents", "/api/config/agents"}:
                return self.reply({"items": [{"id": "default", "revision": 1, "model_policy": {"profile_ref": state["active"]}}]})
            if path == "/api/config/models":
                return self.reply({"items": profiles})
            if path.startswith("/api/config/models/"):
                identity = path.rsplit("/", 1)[-1]
                return self.reply(next(profile for profile in profiles if profile["id"] == identity))
            if path == "/fixture/evidence":
                return self.reply(state)
            if path.startswith("/api/"):
                return self.reply({"items": []})
            return super().do_GET()

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            action = urlparse(self.path).path.rsplit("/", 1)[-1]
            state["evidence"].append({"action": action, "provider_id": body.get("provider_id"), "model_id": body.get("model_id"), "has_api_key": bool(body.get("api_key")), "has_profile_id": bool(body.get("profile_id")), "idempotency": bool(self.headers.get("Idempotency-Key")), "csrf": self.headers.get("X-CSRF-Token") == "fixture-csrf"})
            if body.get("model_id") == "unavailable-model":
                return self.reply({"code": "MODEL_NOT_AVAILABLE", "message": "This error includes fixture-key-canary and must not be displayed"}, 404)
            if action == "discover":
                return self.reply({"items": [{"id": "fixture-discovered-model", "name": "Discovered fixture model"}], "source": "live"})
            if action == "test":
                return self.reply({"connected": True, "tool_calling": True, "message": "Fixture response only"})
            if action == "save":
                old = next((profile for profile in profiles if profile["id"] == body.get("profile_id")), None)
                if body.get("model_id") == "conflict-model" and old:
                    old["revision"] += 1
                    old["model_id"] = "updated-elsewhere-model"
                    return self.reply({"code": "REVISION_CONFLICT", "message": "fixture conflict"}, 409)
                profile = {"id": body.get("profile_id") or f"fixture-{len(profiles)}", "revision": (old or {}).get("revision", 0) + 1, "provider_id": body["provider_id"], "model_id": body["model_id"], "adapter_id": "openai", "credential_ref": "fixture-reference-only" if body.get("api_key") or old and old.get("credential_ref") else None, "endpoint_ref": body.get("base_url", "")}
                profiles[:] = [item for item in profiles if item["id"] != profile["id"]]
                profiles.append(profile)
                state["active"] = f"{profile['id']}@{profile['revision']}"
                return self.reply({"id": profile["id"], "revision": profile["revision"], "profile": profile, "active": True, "message": "Fixture saved"})
            return self.reply({"code": "NOT_FOUND"}, 404)

    print(f"Isolated UI fixture: http://localhost:{args.port} (no real provider calls; separate cookie host)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
