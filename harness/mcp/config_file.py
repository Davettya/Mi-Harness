"""Desired UTF-8 file -> validated atomic DB projection; never connects to MCP."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import portalocker
from pydantic import ValidationError

from harness.core import HarnessError, new_id, utc_now
from harness.platform.credentials import assert_secret_refs
from .models import ConnectionProfile


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class McpConfigFileService:
    def __init__(self, store, data_dir):
        self.store = store
        self.path = Path(data_dir) / "mcp.json"
        self.lock_path = Path(data_dir) / "locks" / "mcp-config.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    def _safe_path(self):
        for path in (self.path, *self.path.parents, self.lock_path, *self.lock_path.parents):
            if path.exists() or path.is_symlink():
                stat = path.lstat()
                if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
                    raise HarnessError("MCP_CONFIG_PATH", "MCP 配置路径不可经过链接或 reparse point", 403)

    def read(self):
        self._safe_path()
        if not self.path.exists():
            return None
        if self.path.stat().st_size > 1024 * 1024:
            raise HarnessError("MCP_CONFIG_SIZE", "MCP 配置超过 1 MiB", 413)
        try:
            return self.path.read_bytes().decode("utf-8")
        except UnicodeError:
            raise HarnessError("MCP_CONFIG_ENCODING", "MCP 配置必须为 UTF-8", 422) from None

    def validate(self, text):
        if len(text.encode("utf-8")) > 1024 * 1024:
            raise HarnessError("MCP_CONFIG_SIZE", "MCP 配置超过 1 MiB", 413)

        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise HarnessError("MCP_JSON_DUPLICATE", "JSON 包含重复字段", 422)
                result[key] = value
            return result

        try:
            data = json.loads(
                text, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
            )
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "MCP_JSON_SYNTAX", f"JSON 语法错误：第 {exc.lineno} 行第 {exc.colno} 列", 422
            ) from None
        except ValueError:
            raise HarnessError("MCP_JSON_SYNTAX", "JSON 不允许非有限数字", 422) from None
        if (
            not isinstance(data, dict)
            or set(data) != {"schema_version", "mcpServers"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or not isinstance(data["mcpServers"], dict)
        ):
            raise HarnessError(
                "MCP_CONFIG_SCHEMA", "需要 schema_version: 1 和 mcpServers 对象，禁止未知字段", 422
            )
        if len(data["mcpServers"]) > 100:
            raise HarnessError("MCP_CONFIG_SIZE", "最多配置 100 个服务", 422)
        result = {}
        for identity, entry in data["mcpServers"].items():
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", identity) or not isinstance(entry, dict):
                raise HarnessError("MCP_CONFIG_SCHEMA", "服务 ID 或配置对象无效", 422)
            if (
                type(entry.get("enabled", False)) is not bool
                or {"id", "revision", "server_id", "config_revision"} & entry.keys()
            ):
                raise HarnessError(
                    "MCP_CONFIG_SCHEMA", f"mcpServers.{identity} 的 enabled 或版本字段无效", 422
                )
            try:
                value = {k: v for k, v in entry.items() if k != "enabled"}
                assert_secret_refs(value)
                profile = ConnectionProfile(server_id=identity, **value)
                for ref in [profile.credential_ref, *profile.environment_refs.values()]:
                    if ref is not None and not ref.startswith(f"credential:mcp-{identity}:"):
                        raise ValueError("credential target mismatch")
            except (ValidationError, ValueError):
                raise HarnessError(
                    "MCP_CONFIG_SCHEMA", f"mcpServers.{identity} 字段无效；仅支持凭据引用及已声明字段", 422
                ) from None
            result[identity] = dict(
                enabled=entry.get("enabled", False), profile=profile.model_dump(mode="json")
            )
        return result

    def initialize(self):
        self._safe_path()
        with portalocker.Lock(str(self.lock_path), timeout=5):
            if self.read() is None and not self.store.get("mcp_file_state", "current"):
                servers = {}
                for item in self.store.list("config/mcp"):
                    identity = item.get("server_id", item.get("id"))
                    servers[identity] = {
                        k: v
                        for k, v in item.items()
                        if k not in {"id", "revision", "server_id", "config_revision"}
                    }
                    servers[identity]["enabled"] = any(
                        identity in a.get("mcp_servers", []) for a in self.store.list("config/agents")
                    )
                text = (
                    json.dumps(dict(schema_version=1, mcpServers=servers), ensure_ascii=False, indent=2)
                    + "\n"
                )
                self.validate(text)
                # Exclusive creation never overwrites a concurrently created external file.
                try:
                    with self.path.open("x", encoding="utf-8", newline="") as stream:
                        stream.write(text)
                        stream.flush()
                        os.fsync(stream.fileno())
                except FileExistsError:
                    pass
        self.reload()

    def _publish(self, text, profiles, journal_id=None):
        source_hash = digest(text)
        with self.store.transaction():
            if self.read() != text:
                raise HarnessError("MCP_FILE_CONFLICT", "配置文件正在变化，请重载；网页草稿已保留", 412)
            previous = self.store.get("mcp_file_state", "current") or {"revision": 0}
            if previous.get("effective_hash") == source_hash:
                return previous
            enabled = []
            for identity, entry in profiles.items():
                old = self.store.get("config/mcp", identity)
                value = entry["profile"]
                unchanged = old and {
                    k: v for k, v in old.items() if k not in {"id", "revision", "config_revision"}
                } == {k: v for k, v in value.items() if k != "config_revision"}
                if not unchanged:
                    revision = (old["revision"] if old else 0) + 1
                    self.store.put(
                        "config/mcp",
                        identity,
                        dict(**{**value, "config_revision": revision}, id=identity, revision=revision),
                    )
                if entry["enabled"]:
                    enabled.append(identity)
            record = dict(
                revision=previous["revision"] + 1,
                effective_hash=source_hash,
                enabled=enabled,
                server_ids=list(profiles),
                loaded_at=utc_now(),
                error=None,
            )
            self.store.put("mcp_file_state", "current", record)
            if journal_id:
                self.store.put("mcp_file_journal", journal_id, dict(hash=source_hash, state="completed"))
            for journal in self.store.list("mcp_file_journal"):
                if journal.get("state") == "prepared":
                    self.store.put(
                        "mcp_file_journal",
                        journal["id"],
                        {
                            **journal,
                            "state": "completed"
                            if journal.get("hash") == source_hash
                            else "superseded_by_disk",
                        },
                    )
            return record

    def reload(self):
        self._safe_path()
        with portalocker.Lock(str(self.lock_path), timeout=5):
            try:
                text = self.read()
                if text is None:
                    raise HarnessError("MCP_FILE_MISSING", "配置文件不存在；保留最后有效配置", 404)
                profiles = self.validate(text)
                self._publish(text, profiles)
                self.store.put("mcp_file_status", "latest", {"error": None})
            except HarnessError as exc:
                self.store.put("mcp_file_status", "latest", {"error": dict(code=exc.code, message=str(exc))})
        return self.view()

    def view(self):
        text = self.read()
        state = self.store.get("mcp_file_state", "current") or {}
        return dict(
            text=text or "",
            path=str(self.path),
            hash=digest(text) if text is not None else None,
            **state,
            load_error=(self.store.get("mcp_file_status", "latest") or {}).get("error"),
        )

    def save(self, text, expected_hash):
        self._safe_path()
        profiles = self.validate(text)
        with portalocker.Lock(str(self.lock_path), timeout=5):
            current = self.read()
            if (digest(current) if current is not None else None) != expected_hash:
                raise HarnessError("MCP_FILE_CONFLICT", "磁盘配置已变化；请核对差异后重试", 412)
            identity = new_id()
            self.store.put(
                "mcp_file_journal",
                identity,
                dict(id=identity, hash=digest(text), previous_hash=expected_hash, state="prepared"),
            )
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".mcp-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(text.encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                if self.read() != current:
                    raise HarnessError("MCP_FILE_CONFLICT", "外部编辑发生在保存期间，请重载", 412)
                os.replace(name, self.path)
                if os.name != "nt":
                    directory = os.open(self.path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                self._publish(text, profiles, identity)
                self.store.put("mcp_file_status", "latest", {"error": None})
            finally:
                Path(name).unlink(missing_ok=True)
        return self.view()

    async def watch(self):
        previous = None
        while True:
            await asyncio.sleep(1)
            try:
                text = self.read()
                current = digest(text) if text is not None else None
                loaded = (self.store.get("mcp_file_state", "current") or {}).get("effective_hash")
                if current == previous and current != loaded:
                    self.reload()
                previous = current
            except (HarnessError, OSError, portalocker.exceptions.LockException):
                # The editor's next stable save is retried, without rewriting its file.
                continue
