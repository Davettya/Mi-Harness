from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterable

from harness.core import ArtifactRef, ExecutionContext, HarnessError, new_id, utc_now
from harness.storage import Store


class ArtifactStore:
    def __init__(self, store: Store, root: Path, max_bytes: int = 128 * 1024 * 1024):
        self.store, self.root, self.max_bytes = store, Path(root), max_bytes
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)

    @staticmethod
    def ref(row: dict) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=row["id"],
            **{
                k: row[k]
                for k in (
                    "content_hash",
                    "mime_type",
                    "size_bytes",
                    "owner_id",
                    "workspace_id",
                    "provenance_ref",
                )
            },
        )

    def put_stream(
        self,
        owner: str,
        workspace: str,
        chunks: Iterable[bytes],
        mime: str = "application/octet-stream",
        provenance: str = "upload",
        display_name: str = "artifact",
    ) -> ArtifactRef:
        self.store.workspace(workspace, owner)
        domain = hashlib.sha256((owner + ":" + workspace).encode()).hexdigest()
        digest, size = hashlib.sha256(), 0
        fd, temp = tempfile.mkstemp(dir=self.root / "tmp")
        temporary = Path(temp)
        try:
            with os.fdopen(fd, "wb") as output:
                for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise HarnessError("ARTIFACT_TOO_LARGE", "产物超过配置大小上限", 413)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            hashed = digest.hexdigest()
            key = f"objects/{domain}/{hashed[:2]}/{hashed}"
            target = self.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._verify(target, hashed, size)
                temporary.unlink()
            else:
                os.replace(temporary, target)
                self._verify(target, hashed, size)
            row = dict(
                id=new_id(),
                owner_id=owner,
                workspace_id=workspace,
                content_hash=hashed,
                mime_type=mime,
                size_bytes=size,
                provenance_ref=provenance,
                storage_key=key,
                display_name=Path(display_name).name,
                created_at=utc_now(),
            )
            self.store.add_artifact(row)
            return self.ref(row)
        finally:
            temporary.unlink(missing_ok=True)

    def put_bytes(
        self,
        owner: str,
        workspace: str,
        data: bytes,
        mime: str = "text/plain",
        provenance: str = "generated",
        display_name: str = "artifact",
    ) -> ArtifactRef:
        return self.put_stream(owner, workspace, [data], mime, provenance, display_name)

    def writer(self, ctx: ExecutionContext, data: bytes, mime: str, provenance: str) -> ArtifactRef:
        ref = self.put_bytes(ctx.owner_id, ctx.workspace_id, data, mime, provenance)
        self.store.reference_artifact(ref.artifact_id, "run", ctx.run_id)
        return ref

    @staticmethod
    def _verify(path: Path, digest: str, size: int):
        if not path.is_file() or path.stat().st_size != size:
            raise HarnessError("ARTIFACT_CORRUPT", "产物对象缺失或大小不匹配", 503)
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise HarnessError("ARTIFACT_CORRUPT", "产物完整性校验失败", 503)

    def path(self, identity: str, owner: str) -> Path:
        row = self.store.artifact(identity, owner)
        target = (self.root / row["storage_key"]).resolve()
        if not target.is_relative_to((self.root / "objects").resolve()):
            raise HarnessError("ARTIFACT_CORRUPT", "非法产物存储位置", 503)
        self._verify(target, row["content_hash"], row["size_bytes"])
        return target

    def read_range(self, identity: str, owner: str, offset: int = 0, length: int = 65536) -> bytes:
        if offset < 0 or not 0 <= length <= self.max_bytes:
            raise HarnessError("INVALID_RANGE", "产物读取范围无效", 422)
        with self.path(identity, owner).open("rb") as source:
            source.seek(offset)
            return source.read(length)

    def integrity(self) -> list[dict]:
        failures = []
        for row in self.store.artifacts():
            try:
                self.path(row["id"], row["owner_id"])
            except HarnessError as exc:
                failures.append(dict(artifact_id=row["id"], code=exc.code))
        return failures

    def gc_preview(self) -> list[str]:
        """Conservative preview only; retained snapshots/backups are never implicitly deleted."""
        referenced = {r["storage_key"] for r in self.store.artifacts()}
        return [
            p.relative_to(self.root).as_posix()
            for p in (self.root / "objects").rglob("*")
            if p.is_file() and p.relative_to(self.root).as_posix() not in referenced
        ]
