"""SQLite repositories. This module alone owns SQL and transaction semantics."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, closing
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from harness.core import ExecutionContext, HarnessError, canonical_json, new_id, utc_now

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS records(namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(namespace,key));
CREATE TABLE IF NOT EXISTS record_versions(namespace TEXT NOT NULL,key TEXT NOT NULL,revision INTEGER NOT NULL,
 payload TEXT NOT NULL,PRIMARY KEY(namespace,key,revision));
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,name TEXT NOT NULL,
 root TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,policy TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 title TEXT NOT NULL,agent_spec_id TEXT NOT NULL,default_branch_id TEXT,revision INTEGER NOT NULL DEFAULT 1,
 archived_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS branches(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),
 parent_id TEXT REFERENCES branches(id),graph_thread_key TEXT NOT NULL UNIQUE,checkpoint_ref TEXT,
 active_run_id TEXT,revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 session_id TEXT NOT NULL REFERENCES sessions(id),branch_id TEXT NOT NULL REFERENCES branches(id),
 root_run_id TEXT NOT NULL,parent_run_id TEXT REFERENCES runs(id),status TEXT NOT NULL CHECK(status IN
 ('queued','running','waiting_user','waiting_children','recovering','needs_review','cancelling','completed','failed','cancelled')),
 revision INTEGER NOT NULL,input_revision INTEGER NOT NULL,snapshot_id TEXT NOT NULL,input TEXT NOT NULL,
 checkpoint_ref TEXT,result_ref TEXT,error TEXT,deadline_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 next_event_seq INTEGER NOT NULL DEFAULT 1,fencing_token INTEGER NOT NULL DEFAULT 0,attempt_id TEXT,
 cancel_requested INTEGER NOT NULL DEFAULT 0,required_interrupts TEXT NOT NULL DEFAULT '[]',depth INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status,created_at);
CREATE INDEX IF NOT EXISTS runs_branch ON runs(branch_id,status);
CREATE INDEX IF NOT EXISTS runs_root ON runs(root_run_id);
CREATE TABLE IF NOT EXISTS run_attempts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
 worker_id TEXT NOT NULL,pid INTEGER NOT NULL,process_start REAL NOT NULL,lease_expiry TEXT NOT NULL,
 fencing_token INTEGER NOT NULL,ended_at TEXT,end_reason TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
 branch_id TEXT NOT NULL REFERENCES branches(id),role TEXT NOT NULL,payload TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_branch ON messages(branch_id,created_at);
CREATE TABLE IF NOT EXISTS tool_executions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
 message_id TEXT NOT NULL,tool_call_id TEXT NOT NULL,tool_id TEXT NOT NULL,tool_revision TEXT NOT NULL,args_hash TEXT NOT NULL,
 args TEXT NOT NULL,resource_fingerprint TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,
 attempt_id TEXT NOT NULL,fencing_token INTEGER NOT NULL,grant_id TEXT,result TEXT,metadata TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(run_id,message_id,tool_call_id));
CREATE TABLE IF NOT EXISTS execution_grants(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES tool_executions(id),
 owner_id TEXT NOT NULL,run_id TEXT NOT NULL,binding TEXT NOT NULL,remaining INTEGER NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,
 consumed_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS interactions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),request_key TEXT NOT NULL,
 interrupt_id TEXT,checkpoint_ref TEXT,input_revision INTEGER NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,payload TEXT NOT NULL,resolution TEXT,expires_at TEXT,created_at TEXT NOT NULL,
 UNIQUE(run_id,request_key),UNIQUE(run_id,interrupt_id,input_revision));
CREATE TABLE IF NOT EXISTS operation_reconciliations(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES tool_executions(id),
 expected_revision INTEGER NOT NULL,actor TEXT NOT NULL,decision TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS budget_accounts(id TEXT PRIMARY KEY,scope TEXT NOT NULL,scope_id TEXT NOT NULL,
 limits TEXT NOT NULL,consumed TEXT NOT NULL DEFAULT '{}',reserved TEXT NOT NULL DEFAULT '{}',UNIQUE(scope,scope_id));
CREATE TABLE IF NOT EXISTS budget_reservations(key TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES budget_accounts(id),
 amount TEXT NOT NULL,actual TEXT,state TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_events(event_id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),seq INTEGER NOT NULL,
 type TEXT NOT NULL,payload TEXT NOT NULL,timestamp TEXT NOT NULL,UNIQUE(run_id,seq));
CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,consumer TEXT NOT NULL,business_key TEXT NOT NULL,payload TEXT NOT NULL,
 delivered_at TEXT,created_at TEXT NOT NULL,UNIQUE(consumer,business_key));
CREATE TABLE IF NOT EXISTS api_commands(owner TEXT NOT NULL,method TEXT NOT NULL,route TEXT NOT NULL,key TEXT NOT NULL,
 body_hash TEXT NOT NULL,response TEXT,status_code INTEGER,expires_at TEXT NOT NULL,PRIMARY KEY(owner,method,route,key));
CREATE TABLE IF NOT EXISTS run_dependencies(parent_id TEXT NOT NULL REFERENCES runs(id),child_id TEXT NOT NULL REFERENCES runs(id),
 delegation_key TEXT NOT NULL UNIQUE,args_hash TEXT NOT NULL,PRIMARY KEY(parent_id,child_id));
CREATE TABLE IF NOT EXISTS run_waits(id TEXT PRIMARY KEY,parent_id TEXT NOT NULL REFERENCES runs(id),request_key TEXT NOT NULL,
 child_ids TEXT NOT NULL,mode TEXT NOT NULL,interrupt_id TEXT,checkpoint_ref TEXT,resolution TEXT,
 revision INTEGER NOT NULL DEFAULT 1,UNIQUE(parent_id,request_key));
CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,workspace_id TEXT NOT NULL REFERENCES workspaces(id),
 content_hash TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,provenance_ref TEXT NOT NULL,
 storage_key TEXT NOT NULL,display_name TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifact_refs(artifact_id TEXT NOT NULL REFERENCES artifacts(id),ref_kind TEXT NOT NULL,
 ref_id TEXT NOT NULL,PRIMARY KEY(artifact_id,ref_kind,ref_id));
CREATE TABLE IF NOT EXISTS input_commands(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),
 input_revision INTEGER NOT NULL,text TEXT NOT NULL,consumed INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 UNIQUE(run_id,input_revision));
CREATE TABLE IF NOT EXISTS ephemeral_frames(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,
 payload TEXT NOT NULL,expires_at TEXT NOT NULL);
"""


def decode(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._active: ContextVar[sqlite3.Connection | None] = ContextVar(f"sql_{id(self)}", default=None)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def migrate(self):
        with closing(self.connect()) as conn:
            existing = conn.execute("SELECT name FROM sqlite_master WHERE name='schema_versions'").fetchone()
            if existing:
                version = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] or 0
                if version > SCHEMA_VERSION:
                    raise HarnessError(
                        "SCHEMA_TOO_NEW", "数据格式比当前运行时新，请使用匹配版本或恢复备份", 503
                    )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_versions VALUES(?,?)", (SCHEMA_VERSION, utc_now()))

    @contextmanager
    def transaction(self, *, readonly: bool = False) -> Iterator["Store"]:
        if self._active.get() is not None:
            yield self
            return
        conn = self.connect()
        token = self._active.set(conn)
        try:
            conn.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
            yield self
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._active.reset(token)
            conn.close()

    def _one(self, sql: str, args=()) -> dict | None:
        with self.transaction(readonly=True):
            return decode(self._active.get().execute(sql, args).fetchone())

    def _all(self, sql: str, args=()) -> list[dict]:
        with self.transaction(readonly=True):
            return [dict(x) for x in self._active.get().execute(sql, args).fetchall()]

    def _exec(self, sql: str, args=()) -> int:
        with self.transaction():
            return self._active.get().execute(sql, args).rowcount

    def get(self, namespace: str, key: str) -> dict | None:
        row = self._one("SELECT payload FROM records WHERE namespace=? AND key=?", (namespace, key))
        return json.loads(row["payload"]) if row else None

    def list(self, namespace: str) -> list[dict]:
        return [
            json.loads(r["payload"])
            for r in self._all("SELECT payload FROM records WHERE namespace=? ORDER BY key", (namespace,))
        ]

    def put(self, namespace: str, key: str, value: dict):
        with self.transaction():
            current = self._one("SELECT revision FROM records WHERE namespace=? AND key=?", (namespace, key))
            self.compare_and_set(namespace, key, current["revision"] if current else 0, value)

    def compare_and_set(self, namespace: str, key: str, expected_revision: int | None, value: dict) -> dict:
        expected_revision = expected_revision or 0
        with self.transaction():
            current = self._one("SELECT revision FROM records WHERE namespace=? AND key=?", (namespace, key))
            if (current["revision"] if current else 0) != expected_revision:
                raise HarnessError("REVISION_CONFLICT", "记录已更新，请刷新后重试")
            value = dict(value)
            revision = expected_revision + 1
            payload = canonical_json(value)
            self._exec(
                "INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(namespace,key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (namespace, key, revision, payload),
            )
            if namespace not in {"system", "process_sessions", "model_permits", "metrics"}:
                self._exec("INSERT INTO record_versions VALUES(?,?,?,?)", (namespace, key, revision, payload))
            return value

    def get_version(self, namespace: str, key: str, revision: int) -> dict | None:
        row = self._one(
            "SELECT payload FROM record_versions WHERE namespace=? AND key=? AND revision=?",
            (namespace, key, revision),
        )
        return json.loads(row["payload"]) if row else None

    def delete(self, namespace: str, key: str):
        self._exec("DELETE FROM records WHERE namespace=? AND key=?", (namespace, key))

    def metrics_facts(self):
        with self.transaction(readonly=True):
            states = {
                r["status"]: r["n"] for r in self._all("SELECT status,COUNT(*) n FROM runs GROUP BY status")
            }
            operations = {
                r["state"]: r["n"]
                for r in self._all("SELECT state,COUNT(*) n FROM tool_executions GROUP BY state")
            }
            queues = [
                r["ms"]
                for r in self._all(
                    "SELECT MAX(0,(julianday(MIN(a.created_at))-julianday(r.created_at))*86400000) ms FROM runs r JOIN run_attempts a ON a.run_id=r.id GROUP BY r.id"
                )
            ]
            queues.sort()
            event_counts = {
                r["type"]: r["n"] for r in self._all("SELECT type,COUNT(*) n FROM run_events GROUP BY type")
            }
            failures = {}
            for row in self._all("SELECT result FROM tool_executions WHERE state IN ('failed','unknown')"):
                code = (json.loads(row["result"]) if row["result"] else {}).get("error_code") or "unknown"
                failures[code] = failures.get(code, 0) + 1
            return dict(
                run_states=states,
                operation_states=operations,
                unknown_operations=operations.get("unknown", 0),
                queue_latency_ms=dict(
                    count=len(queues),
                    p95=queues[min(len(queues) - 1, int(len(queues) * 0.95))] if queues else None,
                ),
                tool_failures=failures,
                budget_reservations={
                    r["state"]: r["n"]
                    for r in self._all("SELECT state,COUNT(*) n FROM budget_reservations GROUP BY state")
                },
                compression_completed=event_counts.get("context.compacted", 0),
                compression_triggered=event_counts.get("context.compaction.started", 0),
                compression_failed=event_counts.get("context.compaction.failed", 0),
                recovery_candidates=states.get("recovering", 0),
                recovery_events=event_counts.get("run.recovering", 0),
                outbox_backlog={
                    r["consumer"]: r["n"]
                    for r in self._all(
                        "SELECT consumer,COUNT(*) n FROM outbox WHERE delivered_at IS NULL GROUP BY consumer"
                    )
                },
            )

    def publish_event_outbox(self):
        # SSE polls the durable event table. Publication therefore requires no live client.
        self._exec(
            "UPDATE outbox SET delivered_at=? WHERE consumer='events' AND delivered_at IS NULL AND business_key IN (SELECT event_id FROM run_events)",
            (utc_now(),),
        )

    def workspace(self, workspace_id: str, owner_id: str | None = None) -> dict:
        row = self._one("SELECT * FROM workspaces WHERE id=?", (workspace_id,))
        if not row or (owner_id and row["owner_id"] != owner_id):
            raise HarnessError("NOT_FOUND", "工作区不存在", 404)
        row["policy"] = json.loads(row["policy"])
        row["roots"] = (self.get("project_roots", row["id"]) or {}).get("roots", [row["root"]])
        return row

    def save_project(self, owner: str, name: str, roots: list[str], policy: dict,
                     identity: str | None = None, expected_revision: int | None = None) -> dict:
        with self.transaction():
            if identity:
                self.workspace(identity, owner)
                active = self._one(
                    "SELECT id FROM runs WHERE workspace_id=? AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                    (identity,),
                )
                if active:
                    raise HarnessError("PROJECT_BUSY", "项目还有运行或待处理任务，请结束后修改源文件夹", 409)
                changed = self._exec(
                    "UPDATE workspaces SET name=?,root=?,revision=revision+1 WHERE id=? AND revision=?",
                    (name, roots[0], identity, expected_revision),
                )
                if not changed:
                    raise HarnessError("REVISION_CONFLICT", "项目已更新，请重新打开项目设置", 409)
            else:
                identity = self.create_workspace(owner, name, roots[0], policy)["id"]
            self.put("project_roots", identity, {"roots": roots})
            return self.workspace(identity, owner)

    def create_workspace(self, owner: str, name: str, root: str, policy: dict) -> dict:
        identity = new_id()
        self._exec(
            "INSERT INTO workspaces VALUES(?,?,?,?,1,?,?)",
            (identity, owner, name, root, canonical_json(policy), utc_now()),
        )
        return self.workspace(identity, owner)

    def workspaces(self, owner: str) -> list[dict]:
        return [
            self.workspace(r["id"])
            for r in self._all("SELECT id FROM workspaces WHERE owner_id=? ORDER BY created_at", (owner,))
        ]

    def create_session(self, workspace_id: str, title: str, agent: str) -> dict:
        sid, bid = new_id(), new_id()
        now = utc_now()
        with self.transaction():
            self._exec(
                "INSERT INTO sessions VALUES(?,?,?,?,?,1,NULL,?,?)",
                (sid, workspace_id, title, agent, bid, now, now),
            )
            self._exec("INSERT INTO branches VALUES(?,?,NULL,?,NULL,NULL,1,?)", (bid, sid, new_id(), now))
        return self.session(sid)

    def session(self, identity: str, owner: str | None = None) -> dict:
        row = self._one("SELECT * FROM sessions WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NOT_FOUND", "会话不存在", 404)
        self.workspace(row["workspace_id"], owner)
        return row

    def sessions(self, workspace_id: str, archived: bool = False) -> list[dict]:
        return self._all(
            "SELECT * FROM sessions WHERE workspace_id=? AND (archived_at IS NOT NULL)=? ORDER BY updated_at DESC",
            (workspace_id, int(archived)),
        )

    def patch_session(self, identity: str, expected: int, patch: dict) -> dict:
        with self.transaction():
            row = self.session(identity)
            count = self._exec(
                "UPDATE sessions SET title=?,archived_at=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (
                    patch.get("title", row["title"]),
                    (utc_now() if patch["archived"] else None) if "archived" in patch else row["archived_at"],
                    utc_now(),
                    identity,
                    expected,
                ),
            )
            if not count:
                raise HarnessError("REVISION_CONFLICT", "会话已更新")
        return self.session(identity)

    def branch(self, identity: str) -> dict:
        row = self._one("SELECT * FROM branches WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NOT_FOUND", "分支不存在", 404)
        row["checkpoint_ref"] = json.loads(row["checkpoint_ref"]) if row["checkpoint_ref"] else None
        return row

    def branches(self, session_id: str) -> list[dict]:
        return [
            self.branch(r["id"])
            for r in self._all(
                "SELECT id FROM branches WHERE session_id=? ORDER BY created_at", (session_id,)
            )
        ]

    def add_branch(
        self,
        session_id: str,
        parent_id: str | None,
        checkpoint: dict | None = None,
        *,
        graph_thread_key: str | None = None,
    ) -> dict:
        identity = new_id()
        self._exec(
            "INSERT INTO branches VALUES(?,?,?,?,?,NULL,1,?)",
            (
                identity,
                session_id,
                parent_id,
                graph_thread_key or new_id(),
                canonical_json(checkpoint) if checkpoint else None,
                utc_now(),
            ),
        )
        return self.branch(identity)

    def advance_branch(self, branch_id: str, expected_revision: int) -> int:
        if not self._exec(
            "UPDATE branches SET revision=revision+1 WHERE id=? AND revision=?",
            (branch_id, expected_revision),
        ):
            raise HarnessError("REVISION_CONFLICT", "分支已更新，请读取最新版本")
        return expected_revision + 1

    def set_branch_owner(self, branch_id: str, expected: str | None, run_id: str | None):
        if not self._exec(
            "UPDATE branches SET active_run_id=? WHERE id=? AND active_run_id IS ?",
            (run_id, branch_id, expected),
        ):
            raise HarnessError("BRANCH_BUSY", "分支仍由另一运行占有")

    def insert_run(self, row: dict) -> dict:
        fields = (
            "id",
            "owner_id",
            "workspace_id",
            "session_id",
            "branch_id",
            "root_run_id",
            "parent_run_id",
            "status",
            "revision",
            "input_revision",
            "snapshot_id",
            "input",
            "deadline_at",
            "created_at",
            "updated_at",
            "depth",
        )
        values = [canonical_json(row[k]) if k == "input" else row[k] for k in fields]
        self._exec(f"INSERT INTO runs({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
        return self.run(row["id"])

    def run(self, identity: str, owner: str | None = None) -> dict:
        row = self._one("SELECT * FROM runs WHERE id=?", (identity,))
        if not row or (owner and row["owner_id"] != owner):
            raise HarnessError("NOT_FOUND", "任务不存在", 404)
        for key in ("input", "checkpoint_ref", "result_ref", "error", "required_interrupts"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
        return row

    def runs(
        self, *, session_id: str | None = None, statuses: list[str] | None = None, root_id: str | None = None
    ) -> list[dict]:
        where, args = [], []
        for key, value in (("session_id", session_id), ("root_run_id", root_id)):
            if value:
                where.append(f"{key}=?")
                args.append(value)
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        clause = " WHERE " + " AND ".join(where) if where else ""
        return [
            self.run(r["id"])
            for r in self._all("SELECT id FROM runs" + clause + " ORDER BY created_at,id", args)
        ]

    def update_run(
        self, identity: str, expected: int, patch: dict, ctx: ExecutionContext | None = None
    ) -> dict:
        allowed = {
            "status",
            "input_revision",
            "checkpoint_ref",
            "result_ref",
            "error",
            "fencing_token",
            "attempt_id",
            "cancel_requested",
            "required_interrupts",
        }
        if not patch.keys() <= allowed:
            raise ValueError("Unsupported run fields")
        with self.transaction():
            if ctx:
                self.assert_fence(ctx, allow_cancelling=True)
            keys = list(patch)
            values = [
                canonical_json(patch[k])
                if k in {"checkpoint_ref", "result_ref", "error", "required_interrupts"}
                and patch[k] is not None
                else patch[k]
                for k in keys
            ]
            count = self._exec(
                f"UPDATE runs SET {','.join(k + '=?' for k in keys)},revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (*values, utc_now(), identity, expected),
            )
            if not count:
                raise HarnessError("REVISION_CONFLICT", "任务已发生变化")
            return self.run(identity)

    def assert_fence(self, ctx: ExecutionContext, *, allow_cancelling: bool = False):
        row = self.run(ctx.run_id)
        attempt = self.attempt(ctx.worker_attempt_id)
        if (
            row["fencing_token"] != ctx.fencing_token
            or row["attempt_id"] != ctx.worker_attempt_id
            or not attempt
            or attempt["ended_at"]
            or attempt["lease_expiry"] <= utc_now()
        ):
            raise HarnessError("STALE_WORKER", "执行租约已失效")
        if row["status"] not in ({"running", "cancelling"} if allow_cancelling else {"running"}) or (
            row["cancel_requested"] and not allow_cancelling
        ):
            raise HarnessError("RUN_STOPPED", "任务已停止接收新操作")

    def add_attempt(self, row: dict):
        self._exec(
            "INSERT INTO run_attempts VALUES(?,?,?,?,?,?,?,NULL,NULL,?)",
            (
                row["id"],
                row["run_id"],
                row["worker_id"],
                row["pid"],
                row["process_start"],
                row["lease_expiry"],
                row["fencing_token"],
                utc_now(),
            ),
        )

    def attempt(self, identity: str) -> dict | None:
        return self._one("SELECT * FROM run_attempts WHERE id=?", (identity,))

    def heartbeat(self, ctx: ExecutionContext, expiry: str):
        with self.transaction():
            self.assert_fence(ctx, allow_cancelling=True)
            self._exec(
                "UPDATE run_attempts SET lease_expiry=? WHERE id=? AND ended_at IS NULL",
                (expiry, ctx.worker_attempt_id),
            )

    def end_attempt(self, identity: str, reason: str):
        self._exec(
            "UPDATE run_attempts SET ended_at=?,end_reason=? WHERE id=? AND ended_at IS NULL",
            (utc_now(), reason, identity),
        )

    def add_message(self, run_id: str, message_id: str, role: str, payload: dict):
        with self.transaction():
            row = self.run(run_id)
            existing = self._one("SELECT payload FROM messages WHERE id=?", (message_id,))
            if existing:
                if existing["payload"] != canonical_json(payload):
                    raise HarnessError("MESSAGE_CONFLICT", "完整消息 ID 已绑定其他内容")
                return
            self._exec(
                "INSERT INTO messages(id,run_id,branch_id,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (message_id, run_id, row["branch_id"], role, canonical_json(payload), utc_now()),
            )
            self.event(run_id, "message.committed", payload)

    def messages(self, branch_id: str, run_id: str | None = None) -> list[dict]:
        sql, args = "SELECT payload FROM messages WHERE branch_id=?", [branch_id]
        if run_id:
            sql += " AND run_id=?"
            args.append(run_id)
        return [json.loads(r["payload"]) for r in self._all(sql + " ORDER BY rowid", args)]

    def event(self, run_id: str, event_type: str, data: dict) -> dict:
        with self.transaction():
            seq = self.run(run_id)["next_event_seq"]
            self._exec("UPDATE runs SET next_event_seq=next_event_seq+1 WHERE id=?", (run_id,))
            eid, now = new_id(), utc_now()
            self._exec(
                "INSERT INTO run_events VALUES(?,?,?,?,?,?)",
                (eid, run_id, seq, event_type, canonical_json(data), now),
            )
            event = dict(
                schema_version=1,
                event_id=eid,
                run_id=run_id,
                type=event_type,
                timestamp=now,
                durability="durable",
                seq=seq,
                data=data,
            )
            self.outbox("events", eid, event)
            return event

    def events(self, run_id: str, after: int = 0, limit: int = 200) -> list[dict]:
        return [
            dict(
                schema_version=1,
                event_id=r["event_id"],
                run_id=run_id,
                type=r["type"],
                timestamp=r["timestamp"],
                durability="durable",
                seq=r["seq"],
                data=json.loads(r["payload"]),
            )
            for r in self._all(
                "SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, limit),
            )
        ]

    def event_range(self, run_id: str) -> tuple[int, int]:
        row = self._one("SELECT MIN(seq) AS first,MAX(seq) AS last FROM run_events WHERE run_id=?", (run_id,))
        return row["first"] or 1, row["last"] or 0

    def outbox(self, consumer: str, key: str, payload: dict):
        self._exec(
            "INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,NULL,?)",
            (new_id(), consumer, key, canonical_json(payload), utc_now()),
        )

    def pending_outbox(self, consumer: str) -> list[dict]:
        rows = self._all(
            "SELECT * FROM outbox WHERE consumer=? AND delivered_at IS NULL ORDER BY created_at LIMIT 200",
            (consumer,),
        )
        return [dict(r, payload=json.loads(r["payload"])) for r in rows]

    def deliver_outbox(self, identity: str):
        self._exec("UPDATE outbox SET delivered_at=? WHERE id=?", (utc_now(), identity))

    def operation(self, identity: str) -> dict:
        row = self._one("SELECT * FROM tool_executions WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NOT_FOUND", "操作不存在", 404)
        for k in ("args", "result", "metadata"):
            row[k] = json.loads(row[k]) if row[k] else None
        return row

    def operation_by_key(self, run_id: str, message_id: str, tool_call_id: str) -> dict | None:
        row = self._one(
            "SELECT id FROM tool_executions WHERE run_id=? AND message_id=? AND tool_call_id=?",
            (run_id, message_id, tool_call_id),
        )
        return self.operation(row["id"]) if row else None

    def prepare_operation(self, ctx: ExecutionContext, row: dict) -> dict:
        with self.transaction():
            self.assert_fence(ctx)
            existing = self.operation_by_key(ctx.run_id, row["message_id"], row["tool_call_id"])
            if existing:
                if (
                    existing["args_hash"] != row["args_hash"]
                    or existing["tool_revision"] != row["tool_revision"]
                    or existing["tool_id"] != row["tool_id"]
                ):
                    raise HarnessError("OPERATION_CONFLICT", "已登记操作的参数或工具版本发生变化")
                return existing
            oid, now = new_id(), utc_now()
            self._exec(
                "INSERT INTO tool_executions(id,run_id,message_id,tool_call_id,tool_id,tool_revision,args_hash,args,resource_fingerprint,state,attempt_id,fencing_token,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?,?,?,?)",
                (
                    oid,
                    ctx.run_id,
                    row["message_id"],
                    row["tool_call_id"],
                    row["tool_id"],
                    row["tool_revision"],
                    row["args_hash"],
                    canonical_json(row["args"]),
                    row["resource_fingerprint"],
                    ctx.worker_attempt_id,
                    ctx.fencing_token,
                    canonical_json(row.get("metadata", {})),
                    now,
                    now,
                ),
            )
            self.event(
                ctx.run_id,
                "tool.prepared",
                dict(
                    operation_id=oid,
                    tool_id=row["tool_id"],
                    tool_revision=row["tool_revision"],
                    input_summary=row.get("input_summary", row["tool_id"]),
                    artifact_refs=[],
                ),
            )
            return self.operation(oid)

    def operations(self, run_id: str) -> list[dict]:
        return [
            self.operation(r["id"])
            for r in self._all("SELECT id FROM tool_executions WHERE run_id=? ORDER BY created_at", (run_id,))
        ]

    def update_operation(
        self, identity: str, expected: int, patch: dict, ctx: ExecutionContext | None = None
    ) -> dict:
        if not patch.keys() <= {
            "state",
            "result",
            "metadata",
            "grant_id",
            "resource_fingerprint",
            "attempt_id",
            "fencing_token",
        }:
            raise ValueError("Unsupported operation update")
        with self.transaction():
            if ctx:
                self.assert_fence(ctx, allow_cancelling=True)
            keys = list(patch)
            values = [canonical_json(patch[k]) if k in {"result", "metadata"} else patch[k] for k in keys]
            if not self._exec(
                f"UPDATE tool_executions SET {','.join(k + '=?' for k in keys)},revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                (*values, utc_now(), identity, expected),
            ):
                raise HarnessError("REVISION_CONFLICT", "操作状态已更新")
            return self.operation(identity)

    def add_grant(self, grant: dict):
        self._exec(
            "INSERT INTO execution_grants VALUES(?,?,?,?,?,?,0,NULL,?)",
            (
                grant["id"],
                grant["operation_id"],
                grant["owner_id"],
                grant["run_id"],
                canonical_json(grant["binding"]),
                grant.get("remaining", 1),
                utc_now(),
            ),
        )

    def consume_grant(
        self, ctx: ExecutionContext, grant_id: str, binding: dict, policy_revision: int
    ) -> dict:
        with self.transaction():
            self.assert_fence(ctx)
            grant = self._one("SELECT * FROM execution_grants WHERE id=?", (grant_id,))
            if (
                not grant
                or grant["owner_id"] != ctx.owner_id
                or grant["run_id"] != ctx.run_id
                or grant["revoked"]
                or grant["remaining"] < 1
            ):
                raise HarnessError("GRANT_INVALID", "授权不可用或已消费", 403)
            saved = json.loads(grant["binding"])
            if (
                saved != binding
                or saved["expires_at"] <= utc_now()
                or saved["policy_revision"] != policy_revision
            ):
                raise HarnessError("GRANT_STALE", "参数、资源或权限已变化，需重新审批", 403)
            operation = self.operation(grant["operation_id"])
            if operation["state"] not in {"prepared", "authorized"}:
                raise HarnessError("OPERATION_STATE", "操作已开始执行")
            self._exec(
                "UPDATE execution_grants SET remaining=remaining-1,consumed_at=? WHERE id=? AND remaining>0",
                (utc_now(), grant_id),
            )
            return self.update_operation(
                operation["id"],
                operation["revision"],
                dict(
                    state="running",
                    grant_id=grant_id,
                    attempt_id=ctx.worker_attempt_id,
                    fencing_token=ctx.fencing_token,
                ),
                ctx,
            )

    def revoke_grants(self, run_id: str, except_ids=()):
        retained = tuple(except_ids)
        if retained:
            self._exec(
                "UPDATE execution_grants SET revoked=1 WHERE run_id=? AND id NOT IN ("
                + ",".join("?" for _ in retained)
                + ")",
                (run_id, *retained),
            )
        else:
            self._exec("UPDATE execution_grants SET revoked=1 WHERE run_id=?", (run_id,))

    def verify_consumed_grant(self, ctx: ExecutionContext, operation_id: str, policy_revision: int) -> bool:
        with self.transaction(readonly=True):
            self.assert_fence(ctx)
            operation = self.operation(operation_id)
            grant = self._one("SELECT * FROM execution_grants WHERE id=?", (operation["grant_id"],))
            if (
                not grant
                or grant["owner_id"] != ctx.owner_id
                or grant["run_id"] != ctx.run_id
                or grant["operation_id"] != operation_id
                or grant["revoked"]
                or grant["remaining"] != 0
                or not grant["consumed_at"]
            ):
                raise HarnessError("GRANT_INVALID", "继续操作的原授权已失效", 403)
            binding = json.loads(grant["binding"])
            if (
                binding["expires_at"] <= utc_now()
                or binding["policy_revision"] != policy_revision
                or binding["args_hash"] != operation["args_hash"]
                or binding["tool_revision"] != operation["tool_revision"]
                or binding["resource_fingerprint"] != operation["resource_fingerprint"]
            ):
                raise HarnessError("GRANT_STALE", "继续操作的参数、资源、期限或权限已变化", 403)
            return True

    def prepare_interaction(
        self, ctx: ExecutionContext, key: str, kind: str, payload: dict, expires_at: str | None
    ) -> dict:
        with self.transaction():
            self.assert_fence(ctx)
            row = self._one("SELECT id FROM interactions WHERE run_id=? AND request_key=?", (ctx.run_id, key))
            if row:
                return self.interaction(row["id"])
            identity = new_id()
            self._exec(
                "INSERT INTO interactions(id,run_id,request_key,input_revision,kind,status,payload,expires_at,created_at) VALUES(?,?,?,?,?,'pending_binding',?,?,?)",
                (
                    identity,
                    ctx.run_id,
                    key,
                    ctx.input_revision,
                    kind,
                    canonical_json(payload),
                    expires_at,
                    utc_now(),
                ),
            )
            return self.interaction(identity)

    def interaction(self, identity: str) -> dict:
        row = self._one("SELECT * FROM interactions WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NOT_FOUND", "交互不存在", 404)
        for k in ("payload", "resolution", "checkpoint_ref"):
            row[k] = json.loads(row[k]) if row[k] else None
        return {**row["payload"], **row, "interaction_id": row["id"]}

    def interactions(self, run_id: str) -> list[dict]:
        return [
            self.interaction(r["id"])
            for r in self._all("SELECT id FROM interactions WHERE run_id=? ORDER BY created_at", (run_id,))
        ]

    def update_interaction(self, identity: str, expected: int, patch: dict) -> dict:
        if not patch.keys() <= {"interrupt_id", "checkpoint_ref", "status", "resolution"}:
            raise ValueError("Unsupported interaction update")
        keys = list(patch)
        values = [
            canonical_json(patch[k]) if k in {"checkpoint_ref", "resolution"} else patch[k] for k in keys
        ]
        if not self._exec(
            f"UPDATE interactions SET {','.join(k + '=?' for k in keys)},revision=revision+1 WHERE id=? AND revision=?",
            (*values, identity, expected),
        ):
            raise HarnessError("REVISION_CONFLICT", "交互已处理或已过期")
        return self.interaction(identity)

    def account(self, identity: str) -> dict:
        row = self._one("SELECT * FROM budget_accounts WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NO_BUDGET", "预算账户不存在")
        return {**row, **{k: json.loads(row[k]) for k in ("limits", "consumed", "reserved")}}

    def add_account(self, identity: str, scope: str, scope_id: str, limits: dict):
        self._exec(
            "INSERT INTO budget_accounts(id,scope,scope_id,limits) VALUES(?,?,?,?)",
            (identity, scope, scope_id, canonical_json(limits)),
        )

    def reserve(self, account_id: str, key: str, amount: dict) -> dict:
        with self.transaction():
            saved = self._one("SELECT * FROM budget_reservations WHERE key=?", (key,))
            if saved:
                if saved["account_id"] != account_id or saved["amount"] != canonical_json(amount):
                    raise HarnessError("RESERVATION_CONFLICT", "同一预算预留键的内容发生变化")
                return saved
            account = self.account(account_id)
            reserved = dict(account["reserved"])
            for dim, value in amount.items():
                if value < 0:
                    raise ValueError("Negative reservation")
                limit = account["limits"].get(dim)
                if (
                    limit is not None
                    and reserved.get(dim, 0) + account["consumed"].get(dim, 0) + value > limit
                ):
                    raise HarnessError("BUDGET_EXHAUSTED", f"{dim} 预算不足", 429)
                reserved[dim] = reserved.get(dim, 0) + value
            self._exec(
                "UPDATE budget_accounts SET reserved=? WHERE id=?", (canonical_json(reserved), account_id)
            )
            self._exec(
                "INSERT INTO budget_reservations VALUES(?,?,?,NULL,'reserved',?)",
                (key, account_id, canonical_json(amount), utc_now()),
            )
            return dict(key=key, account_id=account_id, amount=amount, state="reserved")

    def reservation(self, key: str) -> dict | None:
        return self._one("SELECT * FROM budget_reservations WHERE key=?", (key,))

    def settle(self, key: str, actual: dict | None) -> dict:
        with self.transaction():
            saved = self._one("SELECT * FROM budget_reservations WHERE key=?", (key,))
            if not saved:
                raise HarnessError("NOT_FOUND", "预算预留不存在", 404)
            if saved["state"] == "settled":
                if saved["actual"] != canonical_json(actual):
                    raise HarnessError("SETTLEMENT_CONFLICT", "已结算用量不可覆盖")
                return saved
            if actual is None:
                self._exec("UPDATE budget_reservations SET state='unknown' WHERE key=?", (key,))
                return dict(saved, state="unknown")
            account, amount = self.account(saved["account_id"]), json.loads(saved["amount"])
            reserved, consumed = dict(account["reserved"]), dict(account["consumed"])
            for dim, value in actual.items():
                if value < 0 or value > amount.get(dim, 0):
                    raise HarnessError("USAGE_EXCEEDS_RESERVATION", "用量超过预留，须核对，不能自动退款")
            for dim, value in amount.items():
                reserved[dim] = reserved.get(dim, 0) - value
                consumed[dim] = consumed.get(dim, 0) + actual.get(dim, 0)
            self._exec(
                "UPDATE budget_accounts SET reserved=?,consumed=? WHERE id=?",
                (canonical_json(reserved), canonical_json(consumed), account["id"]),
            )
            self._exec(
                "UPDATE budget_reservations SET state='settled',actual=? WHERE key=?",
                (canonical_json(actual), key),
            )
            return dict(saved, state="settled", actual=actual)

    def dependency(self, key: str) -> dict | None:
        return self._one("SELECT * FROM run_dependencies WHERE delegation_key=?", (key,))

    def add_dependency(self, parent: str, child: str, key: str, args_hash: str):
        self._exec("INSERT INTO run_dependencies VALUES(?,?,?,?)", (parent, child, key, args_hash))

    def children(self, parent: str) -> list[dict]:
        return [
            self.run(r["child_id"])
            for r in self._all("SELECT child_id FROM run_dependencies WHERE parent_id=?", (parent,))
        ]

    def add_wait(self, parent: str, key: str, children: list[str], mode: str) -> dict:
        with self.transaction():
            row = self._one("SELECT id FROM run_waits WHERE parent_id=? AND request_key=?", (parent, key))
            if row:
                return self.wait(row["id"])
            identity = new_id()
            self._exec(
                "INSERT INTO run_waits(id,parent_id,request_key,child_ids,mode) VALUES(?,?,?,?,?)",
                (identity, parent, key, canonical_json(children), mode),
            )
            return self.wait(identity)

    def wait(self, identity: str) -> dict:
        row = self._one("SELECT * FROM run_waits WHERE id=?", (identity,))
        if not row:
            raise HarnessError("NOT_FOUND", "等待对象不存在", 404)
        return {
            **row,
            **{
                k: json.loads(row[k]) if row[k] else None
                for k in ("child_ids", "checkpoint_ref", "resolution")
            },
        }

    def waits(self, parent: str | None = None) -> list[dict]:
        rows = self._all(
            "SELECT id FROM run_waits" + (" WHERE parent_id=?" if parent else ""), (parent,) if parent else ()
        )
        return [self.wait(r["id"]) for r in rows]

    def update_wait(
        self,
        identity: str,
        expected: int,
        interrupt_id: str | None,
        checkpoint: dict | None,
        resolution: dict | None,
    ):
        if not self._exec(
            "UPDATE run_waits SET interrupt_id=?,checkpoint_ref=?,resolution=?,revision=revision+1 WHERE id=? AND revision=?",
            (
                interrupt_id,
                canonical_json(checkpoint) if checkpoint else None,
                canonical_json(resolution) if resolution else None,
                identity,
                expected,
            ),
        ):
            raise HarnessError("REVISION_CONFLICT", "等待状态已更新")

    def api_command(self, owner: str, method: str, route: str, key: str) -> dict | None:
        row = self._one(
            "SELECT * FROM api_commands WHERE owner=? AND method=? AND route=? AND key=?",
            (owner, method, route, key),
        )
        return dict(row, response=json.loads(row["response"]) if row["response"] else None) if row else None

    def save_api_command(
        self,
        owner: str,
        method: str,
        route: str,
        key: str,
        body_hash: str,
        response: Any,
        status: int,
        expiry: str,
    ):
        self._exec(
            "INSERT INTO api_commands VALUES(?,?,?,?,?,?,?,?)",
            (owner, method, route, key, body_hash, canonical_json(response), status, expiry),
        )

    def complete_api_command(self, owner: str, method: str, route: str, key: str, response: Any, status: int):
        self._exec(
            "UPDATE api_commands SET response=?,status_code=? WHERE owner=? AND method=? AND route=? AND key=?",
            (canonical_json(response), status, owner, method, route, key),
        )

    def add_artifact(self, row: dict):
        fields = (
            "id",
            "owner_id",
            "workspace_id",
            "content_hash",
            "mime_type",
            "size_bytes",
            "provenance_ref",
            "storage_key",
            "display_name",
            "created_at",
        )
        self._exec(
            f"INSERT INTO artifacts({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
            [row[k] for k in fields],
        )

    def artifact(self, identity: str, owner: str | None = None) -> dict:
        row = self._one("SELECT * FROM artifacts WHERE id=?", (identity,))
        if not row or (owner and row["owner_id"] != owner):
            raise HarnessError("NOT_FOUND", "产物不存在", 404)
        return row

    def artifacts(self, workspace_id: str | None = None) -> list[dict]:
        return self._all(
            "SELECT * FROM artifacts" + (" WHERE workspace_id=?" if workspace_id else ""),
            (workspace_id,) if workspace_id else (),
        )

    def reference_artifact(self, identity: str, kind: str, target: str):
        self._exec("INSERT OR IGNORE INTO artifact_refs VALUES(?,?,?)", (identity, kind, target))

    def run_artifacts(self, run_id: str) -> list[dict]:
        return self._all(
            "SELECT a.* FROM artifacts a JOIN artifact_refs r ON r.artifact_id=a.id WHERE r.ref_kind='run' AND r.ref_id=?",
            (run_id,),
        )

    def snapshot(self, run_id: str, owner: str) -> dict:
        with self.transaction(readonly=True):
            run = self.run(run_id, owner)
            return {
                **run,
                "run_id": run_id,
                "last_seq": run["next_event_seq"] - 1,
                "messages": self.messages(run["branch_id"]),
                "tools": self.operations(run_id),
                "interactions": [r for r in self.interactions(run_id) if r["status"] != "pending_binding"],
                "artifacts": self.run_artifacts(run_id),
                "children": self.children(run_id),
            }

    def add_reconciliation(
        self, operation_id: str, expected_revision: int, actor: str, decision: str, payload: dict
    ) -> str:
        identity = new_id()
        self._exec(
            "INSERT INTO operation_reconciliations VALUES(?,?,?,?,?,?,?)",
            (identity, operation_id, expected_revision, actor, decision, canonical_json(payload), utc_now()),
        )
        return identity

    def reconciliations(self, operation_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM operation_reconciliations WHERE operation_id=? ORDER BY created_at",
            (operation_id,),
        )

    def add_input_command(self, run_id: str, revision: int, text: str) -> dict:
        identity = new_id()
        self._exec(
            "INSERT INTO input_commands VALUES(?,?,?,?,0,?)", (identity, run_id, revision, text, utc_now())
        )
        return dict(id=identity, run_id=run_id, input_revision=revision, text=text)

    def consume_input_commands(self, ctx: ExecutionContext) -> list[dict]:
        with self.transaction():
            self.assert_fence(ctx)
            rows = self._all(
                "SELECT * FROM input_commands WHERE run_id=? AND consumed=0 ORDER BY input_revision",
                (ctx.run_id,),
            )
            self._exec("UPDATE input_commands SET consumed=1 WHERE run_id=? AND consumed=0", (ctx.run_id,))
            return rows

    def pending_input_commands(self, run_id: str) -> list[dict]:
        return self._all(
            "SELECT * FROM input_commands WHERE run_id=? AND consumed=0 ORDER BY input_revision", (run_id,)
        )

    def ack_input_commands(self, ctx: ExecutionContext, identities: list[str]):
        with self.transaction():
            self.assert_fence(ctx)
            run = self.run(ctx.run_id)
            if ctx.input_revision > run["input_revision"]:
                self.update_run(ctx.run_id, run["revision"], dict(input_revision=ctx.input_revision), ctx)
            for identity in identities:
                self._exec(
                    "UPDATE input_commands SET consumed=1 WHERE id=? AND run_id=?", (identity, ctx.run_id)
                )

    def publish_ephemeral(self, run_id: str, data: dict):
        from datetime import datetime, timedelta, timezone

        expiry = (
            (datetime.now(timezone.utc) + timedelta(minutes=5))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        event = dict(
            schema_version=1,
            event_id=new_id(),
            run_id=run_id,
            type="message.delta",
            timestamp=utc_now(),
            durability="ephemeral",
            seq=None,
            data=data,
        )
        with self.transaction():
            self._exec(
                "INSERT INTO ephemeral_frames(run_id,payload,expires_at) VALUES(?,?,?)",
                (run_id, canonical_json(event), expiry),
            )
            self._exec("DELETE FROM ephemeral_frames WHERE expires_at<?", (utc_now(),))

    def ephemeral_cursor(self, run_id: str) -> int:
        row = self._one("SELECT MAX(id) AS last FROM ephemeral_frames WHERE run_id=?", (run_id,))
        return row["last"] or 0

    def read_ephemeral(self, run_id: str, after: int, limit: int = 128) -> list[dict]:
        return [
            dict(id=r["id"], event=json.loads(r["payload"]))
            for r in self._all(
                "SELECT id,payload FROM ephemeral_frames WHERE run_id=? AND id>? AND expires_at>? ORDER BY id LIMIT ?",
                (run_id, after, utc_now(), limit),
            )
        ]

    def integrity(self) -> dict:
        with closing(self.connect()) as conn:
            checks = [x[0] for x in conn.execute("PRAGMA integrity_check")]
            foreign = [tuple(x) for x in conn.execute("PRAGMA foreign_key_check")]
        return dict(
            ok=checks == ["ok"] and not foreign,
            integrity=checks,
            foreign_keys=foreign,
            schema_version=SCHEMA_VERSION,
        )
