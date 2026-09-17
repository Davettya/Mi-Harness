"""Reviewable memory with revision CAS; retrieval filters access before ranking."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from harness.core import HarnessError

from .models import MemoryRecord


def owner_of(identity) -> str:
    if isinstance(identity, str):
        return identity
    if isinstance(identity, dict):
        return identity["owner_id"]
    return identity.owner_id


def expired(record: MemoryRecord) -> bool:
    if not record.expires_at:
        return False
    return datetime.fromisoformat(record.expires_at) <= datetime.now(UTC)


class MemoryService:
    def __init__(self, repository, access_check=None):
        self.repository = repository
        self.access_check = access_check

    def _check(self, identity, record: MemoryRecord):
        if owner_of(identity) != record.owner:
            raise HarnessError("memory_not_found", "Memory record is not accessible", 404)
        workspace = (
            identity.get("workspace_id")
            if isinstance(identity, dict)
            else getattr(identity, "workspace_id", None)
        )
        if workspace and record.scope == "workspace" and workspace != record.workspace:
            raise HarnessError("memory_not_found", "Memory record is not accessible", 404)
        if self.access_check:
            self.access_check(identity, record.workspace)

    def propose(
        self, identity, workspace_id: str, content: str, source_refs: list[str | dict], **fields
    ) -> MemoryRecord:
        if self.access_check:
            self.access_check(identity, workspace_id)
        allowed = {"scope", "kind", "expires_at", "confidence"}
        if set(fields) - allowed:
            raise HarnessError("invalid_memory_field", "Memory proposals contain unsupported fields", 422)
        record = MemoryRecord(
            owner=owner_of(identity),
            workspace=workspace_id,
            content=content,
            source_refs=source_refs,
            **fields,
        )
        self.repository.put("memory", record.id, record.model_dump(mode="json"))
        return record

    def get(self, identity, memory_id: str) -> MemoryRecord:
        data = self.repository.get("memory", memory_id)
        if not data:
            raise HarnessError("memory_not_found", "Memory record was not found", 404)
        record = MemoryRecord.model_validate(data)
        self._check(identity, record)
        if record.review_state == "deleted":
            raise HarnessError("memory_not_found", "Memory record was deleted", 404)
        return record

    def list(self, identity, workspace_id: str, filters=None, cursor=None, limit=50):
        if self.access_check:
            self.access_check(identity, workspace_id)
        filters = filters or {}
        records = []
        for data in self.repository.list("memory"):
            record = MemoryRecord.model_validate(data)
            if record.owner != owner_of(identity) or record.review_state == "deleted":
                continue
            if record.scope == "workspace" and record.workspace != workspace_id:
                continue
            if any(getattr(record, k, None) != v for k, v in filters.items()):
                continue
            records.append(record)
        records.sort(key=lambda r: (r.created_at, r.id))
        try:
            offset = int(cursor or 0)
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise HarnessError("invalid_cursor", "Memory pagination cursor or limit is invalid", 422) from exc
        return {
            "items": records[offset : offset + limit],
            "next_cursor": str(offset + limit) if offset + limit < len(records) else None,
        }

    def _replace(self, old: MemoryRecord, patch: dict, expected_revision: int) -> MemoryRecord:
        if old.revision != expected_revision:
            raise HarnessError("revision_conflict", "Memory was modified; refresh before editing")
        new = MemoryRecord.model_validate({**old.model_dump(), **patch, "revision": old.revision + 1})
        result = self.repository.compare_and_set(
            "memory", old.id, expected_revision, new.model_dump(mode="json")
        )
        if result is False:
            raise HarnessError("revision_conflict", "Memory was modified; refresh before editing")
        # No vector cache exists: every retrieval reads committed authoritative records.
        return new

    def review(self, identity, memory_id, decision, expected_revision):
        if decision not in {"accepted", "rejected", "accept", "reject"}:
            raise HarnessError("invalid_decision", "Review decision must accept or reject a proposal", 422)
        record = self.get(identity, memory_id)
        if record.review_state != "proposed":
            raise HarnessError("invalid_memory_state", "Only proposed memory can be reviewed")
        return self._replace(
            record,
            {"review_state": {"accept": "accepted", "reject": "rejected"}.get(decision, decision)},
            expected_revision,
        )

    def update(self, identity, memory_id, patch, expected_revision):
        if set(patch) - {"content", "source_refs", "expires_at"}:
            raise HarnessError(
                "immutable_memory_field", "Only content, source references and expiry may be edited", 422
            )
        return self._replace(self.get(identity, memory_id), patch, expected_revision)

    def delete(self, identity, memory_id, expected_revision):
        record = self._replace(self.get(identity, memory_id), {"review_state": "deleted"}, expected_revision)
        return {"memory_id": record.id, "revision": record.revision, "deleted": True}

    def retrieve(self, identity, workspace_id, query: str, limit=10):
        # Authorization filter is deliberately performed before tokenization/scoring.
        page = self.list(identity, workspace_id, {"review_state": "accepted"}, limit=100)
        candidates = page["items"]
        while page["next_cursor"]:
            page = self.list(
                identity, workspace_id, {"review_state": "accepted"}, cursor=page["next_cursor"], limit=100
            )
            candidates.extend(page["items"])
        terms = set(re.findall(r"\w+", query.lower()))
        terms.update(query[i : i + 2] for i in range(len(query) - 1) if "\u4e00" <= query[i] <= "\u9fff")
        scored = [(sum(term in r.content.lower() for term in terms), r) for r in candidates if not expired(r)]
        scored.sort(key=lambda item: (item[1].kind == "pinned", item[0], item[1].created_at), reverse=True)
        return [r for score, r in scored if score or r.kind == "pinned"][: max(1, min(limit, 100))]
