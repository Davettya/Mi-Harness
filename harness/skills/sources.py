from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist"}


def safe_relative(value: str) -> str:
    # Backslashes, drives, ADS and UNC are invalid even on POSIX hosts.
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or path.is_absolute() or ".." in path.parts:
        raise PermissionError("resource reference escapes its skill snapshot")
    if value != path.as_posix() or value == ".":
        raise PermissionError("resource reference must be canonical")
    return value


def bundle_hash(files: dict[str, bytes]) -> str:
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class SkillCandidate:
    relative_path: str
    root_uri: str


class LocalSkillSource:
    def __init__(self, source_id: str, root: Path, *, scope: str = "project", trusted: bool = False,
                 max_depth: int = 6, max_file_bytes: int = 2_000_000,
                 max_bundle_bytes: int = 16_000_000, max_files: int = 512,
                 path_validator: Callable[[Path], None] | None = None):
        self.source_id, self.root, self.scope = source_id, Path(root).resolve(), scope
        self.trusted, self.enabled = trusted, True
        self.max_depth, self.max_file_bytes = max_depth, max_file_bytes
        self.max_bundle_bytes, self.max_files = max_bundle_bytes, max_files
        self.path_validator = path_validator

    def _check(self, path: Path) -> Path:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise PermissionError("skill path escapes configured source")
        # Reject all links/junctions, including same-root links: no alias races.
        for ancestor in [path, *path.parents]:
            if ancestor == self.root.parent:
                break
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                raise PermissionError("skill links and junctions are not allowed")
        if self.path_validator:
            self.path_validator(resolved)
        return resolved

    def discover(self, scope: str | None = None):
        if not self.enabled or (scope and scope != self.scope):
            return
        if not self.root.is_dir():
            return
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            current = Path(directory)
            depth = len(current.relative_to(self.root).parts)
            dirs[:] = sorted(d for d in dirs if d not in IGNORED and not (current / d).is_symlink()
                             and not (hasattr(current / d, "is_junction") and (current / d).is_junction())
                             and depth < self.max_depth)
            if "SKILL.md" in files:
                yield SkillCandidate(current.relative_to(self.root).as_posix(), current.as_uri())

    def snapshot(self, candidate: SkillCandidate) -> dict[str, bytes]:
        base = self.root if candidate.relative_path == "." else self.root / safe_relative(candidate.relative_path)
        self._check(base)
        result: dict[str, bytes] = {}
        total = 0
        for directory, dirs, files in os.walk(base, followlinks=False):
            current = Path(directory)
            dirs[:] = sorted(d for d in dirs if d not in IGNORED)
            if len(current.relative_to(base).parts) > self.max_depth:
                raise ValueError("skill bundle scan depth exceeded")
            for name in sorted(files):
                path = self._check(current / name)
                relative = safe_relative((current / name).relative_to(base).as_posix())
                with path.open("rb") as stream:
                    data = stream.read(self.max_file_bytes + 1)
                if len(data) > self.max_file_bytes:
                    raise ValueError(f"skill file exceeds byte limit: {relative}")
                total += len(data)
                if total > self.max_bundle_bytes or len(result) >= self.max_files:
                    raise ValueError("skill bundle exceeds total resource limit")
                result[relative] = data
        if "SKILL.md" not in result:
            raise ValueError("SKILL.md missing from bundle")
        return result


class RemoteSkillSource:
    """Remote URI handling is supplied by the MCP resource port, never Path(uri)."""
    EXTENSION = "io.modelcontextprotocol/skills"

    def __init__(self, source_id: str, *, capability: dict, discover, fetch_bundle,
                 expected_revision: str, trusted: bool = False, max_bundle_bytes: int = 16_000_000):
        extension = capability.get(self.EXTENSION)
        if not isinstance(extension, dict) or extension.get("revision") != expected_revision:
            raise ValueError("remote skills extension absent or revision not verified")
        self.source_id, self.scope, self.enabled = source_id, "remote", True
        self.trusted, self._discover, self._fetch = trusted, discover, fetch_bundle
        self.revision, self.max_bundle_bytes = expected_revision, max_bundle_bytes

    def discover(self, scope=None):
        if self.enabled and scope in (None, "remote"):
            yield from self._discover()

    def snapshot(self, candidate):
        files = self._fetch(candidate)
        if "SKILL.md" not in files or len(files) > 512 or sum(map(len, files.values())) > self.max_bundle_bytes:
            raise ValueError("invalid or oversized remote skill bundle")
        for name, data in files.items():
            safe_relative(name)
            if not isinstance(data, bytes):
                raise ValueError("remote skill resource must be bytes")
        return files
