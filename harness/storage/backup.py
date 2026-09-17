from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from importlib.metadata import version
from contextlib import closing
from pathlib import Path

from harness.core import HarnessError, canonical_json, utc_now
from .store import SCHEMA_VERSION, Store


def backup(data_dir: Path, destination: Path, *, writers_stopped: bool) -> dict:
    if not writers_stopped:
        raise HarnessError("MAINTENANCE_REQUIRED", "备份前必须停止业务与 checkpoint 写入")
    if destination.exists() and any(destination.iterdir()):
        raise HarnessError("DESTINATION_NOT_EMPTY", "备份目录必须为空")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("app.db", "checkpoints.db"):
        source = data_dir / name
        if source.exists():
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination / name)) as dst:
                src.backup(dst)
    objects = data_dir / "artifacts"
    if objects.exists():
        shutil.copytree(
            objects, destination / "artifacts", dirs_exist_ok=True, ignore=shutil.ignore_patterns("tmp")
        )
    snapshots = data_dir / "skill-snapshots"
    if snapshots.exists():
        shutil.copytree(snapshots, destination / "skill-snapshots", dirs_exist_ok=True)
    manifest = dict(
        schema_version=SCHEMA_VERSION,
        application_version=version("local-agent-harness"),
        checkpoint_saver_version=version("langgraph-checkpoint-sqlite"),
        manifest_version=1,
        created_at=utc_now(),
        files={},
        external_effects_rolled_back=False,
    )
    for path in destination.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            manifest["files"][path.relative_to(destination).as_posix()] = dict(
                sha256=digest, size=path.stat().st_size
            )
    (destination / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    return manifest


def restore(backup_dir: Path, destination: Path) -> dict:
    if destination.exists() and any(destination.iterdir()):
        raise HarnessError("DESTINATION_NOT_EMPTY", "恢复必须写入新的空目录")
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest["schema_version"] > SCHEMA_VERSION:
        raise HarnessError("SCHEMA_TOO_NEW", "备份数据格式不兼容", 503)
    if (
        manifest.get("manifest_version", 1) != 1
        or manifest.get("checkpoint_saver_version", version("langgraph-checkpoint-sqlite")).split(".")[0]
        != version("langgraph-checkpoint-sqlite").split(".")[0]
    ):
        raise HarnessError("CHECKPOINT_VERSION", "checkpoint saver 或备份 manifest 版本不兼容", 503)
    if "app.db" not in manifest["files"]:
        raise HarnessError("BACKUP_CORRUPT", "备份缺少业务数据库", 503)
    for name, metadata in manifest["files"].items():
        source = (backup_dir / name).resolve()
        if not source.is_relative_to(backup_dir.resolve()) or not source.is_file():
            raise HarnessError("BACKUP_CORRUPT", "备份对象缺失或路径非法", 503)
        with source.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if source.stat().st_size != metadata["size"] or digest != metadata["sha256"]:
            raise HarnessError("BACKUP_CORRUPT", "备份摘要不匹配", 503)
    destination.mkdir(parents=True, exist_ok=True)
    for name in manifest["files"]:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_dir / name, target)
    store = Store(destination / "app.db")
    report = store.integrity()
    if not report["ok"]:
        raise HarnessError("BACKUP_CORRUPT", "恢复后数据库完整性校验失败", 503)
    from harness.artifacts import ArtifactStore

    if ArtifactStore(store, destination / "artifacts").integrity():
        raise HarnessError("ARTIFACT_CORRUPT", "恢复后的数据库引用存在缺失或损坏产物", 503)
    checkpoint = destination / "checkpoints.db"
    if checkpoint.exists():
        with closing(sqlite3.connect(checkpoint)) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HarnessError("CHECKPOINT_CORRUPT", "恢复后 checkpoint 无法读取", 503)
    return dict(**report, external_effects_rolled_back=False)
