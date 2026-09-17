"""Record source/fixture/lock identity and local environment without user data."""

import hashlib
import importlib.metadata
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = sorted(
    p
    for base in ("harness", "tests", "apps/web/src", "evals", "scripts")
    for p in (root / base).rglob("*")
    if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc"}
)
hashes = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
evidence = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "source_revision": None,
    "source_revision_note": "Workspace has no Git repository; SHA256 file manifest identifies tested sources",
    "source_tree_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
    "files": hashes,
    "os": platform.platform(),
    "architecture": platform.machine(),
    "python": platform.python_version(),
    "dependencies": {
        name: importlib.metadata.version(name)
        for name in (
            "langchain",
            "langgraph",
            "langgraph-checkpoint-sqlite",
            "langchain-openai",
            "langchain-anthropic",
            "langchain-ollama",
            "mcp",
            "fastapi",
            "pydantic",
        )
    },
    "locks": {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("uv.lock", "apps/web/package-lock.json")
    },
    "fixture_scope": "loopback protocol servers and isolated temporary workspaces; no external provider acceptance",
    "hardware": {"processor": platform.processor()},
    "secrets_exported": False,
}
destination = root / "docs/verification/source-manifest.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
print(destination)
