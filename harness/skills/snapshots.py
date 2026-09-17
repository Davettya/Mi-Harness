from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from .sources import bundle_hash, safe_relative


class SnapshotStore:
    """Immutable bundles published after a durable manifest and complete file writes."""
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, content_hash: str) -> Path:
        if len(content_hash) != 64 or any(c not in "0123456789abcdef" for c in content_hash):
            raise ValueError("invalid snapshot hash")
        return self.root / content_hash

    def put(self, files: dict[str, bytes]) -> str:
        digest = bundle_hash(files)
        target = self._path(digest)
        if target.exists():
            self.verify(digest)
            return digest
        temporary = Path(tempfile.mkdtemp(prefix=".skill-", dir=self.root))
        try:
            manifest = {}
            for name, content in files.items():
                path = temporary / "files" / safe_relative(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as out:
                    out.write(content)
                    out.flush()
                    os.fsync(out.fileno())
                manifest[name] = hashlib.sha256(content).hexdigest()
            with (temporary / "manifest.json").open("x", encoding="utf-8") as out:
                json.dump(manifest, out, sort_keys=True, separators=(",", ":"))
                out.flush()
                os.fsync(out.fileno())
            try:
                temporary.rename(target)
            except FileExistsError:
                self.verify(digest)
            return digest
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def manifest(self, digest: str) -> dict[str, str]:
        path = self._path(digest)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise PermissionError("snapshot link is forbidden")
        try:
            manifest = json.loads((path / "manifest.json").read_text("utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError("pinned skill snapshot is missing") from exc
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(encoded).hexdigest() != digest:
            raise ValueError("skill snapshot manifest integrity failure")
        return manifest

    def read(self, digest: str, resource_ref: str) -> bytes:
        name = safe_relative(resource_ref)
        manifest = self.manifest(digest)
        if name not in manifest:
            raise FileNotFoundError("resource is not in pinned skill snapshot")
        base = self._path(digest) / "files"
        path = base / name
        if not path.resolve().is_relative_to(base.resolve()):
            raise PermissionError("snapshot resource escaped bundle")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != manifest[name]:
            raise ValueError("skill resource integrity failure")
        return content

    def verify(self, digest: str):
        for name in self.manifest(digest):
            self.read(digest, name)
