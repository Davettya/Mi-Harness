"""Export the real FastAPI route schema without starting workers or creating user data."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from harness.server.app import create_app  # noqa: E402

services = SimpleNamespace(
    settings=SimpleNamespace(host="127.0.0.1", port=8767),
    store=None,
    artifacts=SimpleNamespace(max_bytes=128 * 1024 * 1024),
)
schema = create_app(services).openapi()
target = root / "docs" / "generated" / "openapi.json"
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
print(target)
