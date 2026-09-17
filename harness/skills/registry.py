from __future__ import annotations

import difflib
import threading
import uuid
from typing import Callable
from .models import RefreshReport, SkillRecord, StateRepository
from .parser import parse_skill
from .snapshots import SnapshotStore


class SkillRegistry:
    def __init__(self, sources, snapshots: SnapshotStore, repository: StateRepository,
                 authorize: Callable | None = None):
        self.sources = {source.source_id: source for source in sources}
        self.snapshots, self.repository = snapshots, repository
        self.authorize = authorize or self._default_authorize
        self._lock = threading.RLock()

    @staticmethod
    def _default_authorize(action, context, record):
        if record.trust_state != "trusted":
            raise PermissionError("skill source requires explicit trust review")

    def get(self, skill_id: str) -> SkillRecord:
        item = self.repository.get("skills", skill_id)
        if not item:
            raise KeyError("unknown skill")
        return SkillRecord.model_validate(item)

    def refresh(self, source_ids=None) -> RefreshReport:
        report = RefreshReport()
        with self._lock:
            existing = {(r["source_id"], r["relative_path"]): r for r in self.repository.list("skills")}
            for source_id in source_ids or self.sources:
                source = self.sources[source_id]
                seen = set()
                for candidate in source.discover():
                    seen.add(candidate.relative_path)
                    old = existing.get((source_id, candidate.relative_path))
                    metadata, diagnostics, digest = {}, [], ""
                    try:
                        files = source.snapshot(candidate)
                        metadata, _, diagnostics = parse_skill(files["SKILL.md"].decode("utf-8"))
                        digest = self.snapshots.put(files)
                    except (ValueError, PermissionError, OSError, UnicodeError) as exc:
                        diagnostics = [f"{type(exc).__name__}: {exc}"]
                    record = SkillRecord(
                        skill_id=old["skill_id"] if old else str(uuid.uuid4()),
                        source_id=source_id, relative_path=candidate.relative_path,
                        name=metadata.get("name") if isinstance(metadata.get("name"), str) else candidate.relative_path,
                        description=metadata.get("description") if isinstance(metadata.get("description"), str) else "",
                        root_uri=candidate.root_uri, content_hash=digest, scope=source.scope,
                        revision=old["revision"] + 1 if old else 1,
                        enabled=bool(not diagnostics and (old.get("enabled", True) if old else True)),
                        trust_state=("blocked" if old and old.get("trust_state")=="blocked" else
                                     "trusted" if source.trusted or (old and old.get("trust_state")=="trusted") else "unreviewed"),
                        validation_diagnostics=diagnostics,
                        license=metadata.get("license") if isinstance(metadata.get("license"), str) else None,
                        compatibility=metadata.get("compatibility") if isinstance(metadata.get("compatibility"), str) else None,
                        dependency_metadata=metadata.get("metadata") if isinstance(metadata.get("metadata"), dict) else {},
                    )
                    self.repository.put("skills", record.skill_id, record.model_dump(mode="json"))
                    if digest:
                        self.repository.put("skill_versions", record.skill_id + ":" + digest, record.model_dump(mode="json"))
                    report.refreshed.append(record)
                # Removed records stay inspectable and old snapshots stay available.
                for (sid, path), item in existing.items():
                    if sid == source_id and path not in seen:
                        item = dict(item, enabled=False, revision=item["revision"] + 1,
                                    validation_diagnostics=["source entry removed or source disabled"])
                        self.repository.put("skills", item["skill_id"], item)
            names: dict[str, list[str]] = {}
            for row in self.repository.list("skills"):
                names.setdefault(row["name"], []).append(row["skill_id"])
            report.name_conflicts = {name: ids for name, ids in names.items() if len(ids) > 1}
        return report

    def search(self, query: str, execution_context, *, allowlist: list[str] | None = None):
        matches = []
        for item in self.repository.list("skills"):
            record = SkillRecord.model_validate(item)
            if not record.enabled or record.validation_diagnostics or (allowlist is not None and record.skill_id not in allowlist):
                continue
            if record.source_id in self.sources and not self.sources[record.source_id].enabled:
                continue
            if query.casefold() not in (record.name + " " + record.description).casefold():
                continue
            try:
                self.authorize("discover", execution_context, record)
            except PermissionError:
                continue
            matches.append(record)
        return matches

    def configure(self, skill_id: str, *, expected_revision: int, enabled: bool | None = None,
                  trust_state: str | None = None):
        with self._lock:
            record = self.get(skill_id)
            if record.revision != expected_revision:
                raise ValueError("skill configuration revision conflict")
            update = {"revision": expected_revision + 1}
            if enabled is not None:
                if enabled and record.validation_diagnostics:
                    raise ValueError("invalid skill cannot be enabled")
                update["enabled"] = enabled
            if trust_state is not None:
                update["trust_state"] = trust_state
            record = SkillRecord.model_validate(record.model_dump() | update)
            self.repository.compare_and_set("skills", skill_id, expected_revision, record.model_dump(mode="json"))
            return record

    def preview(self, skill_id: str, context, content_hash: str | None = None):
        record = self.get(skill_id)
        self.authorize("read", context, record)
        digest = content_hash or record.content_hash
        if not self.repository.get("skill_versions", skill_id + ":" + digest):
            raise PermissionError("snapshot does not belong to this skill")
        return self.snapshots.read(digest, "SKILL.md").decode("utf-8")

    def diff(self, skill_id: str, old_hash: str, new_hash: str, context):
        old = self.preview(skill_id, context, old_hash).splitlines(keepends=True)
        new = self.preview(skill_id, context, new_hash).splitlines(keepends=True)
        return "".join(difflib.unified_diff(old, new, fromfile=old_hash, tofile=new_hash))
