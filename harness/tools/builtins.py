from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit

import urllib3

from harness.core import HarnessError, OperationKey, content_hash, utc_now

from .contracts import ToolSpec, ToolWait
from .gateway import after_seconds


def schema(properties: dict, required: list[str] | None = None):
    return dict(type="object", properties=properties, required=required or [], additionalProperties=False)


TEXT = {"type": "string"}
PATH = {"type": "string", "minLength": 1}
INTEGER = {"type": "integer", "minimum": 0}

SEARCH_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".idea", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__", "node_modules"}
)
SEARCH_FILE_LIMIT = 20000
SEARCH_FILE_MAX_BYTES = 2 * 1024 * 1024
SEARCH_LINE_MAX_CHARS = 1000


@contextmanager
def directory_guards(path: Path, root: Path):
    """Deny delete/rename of each parent directory while opening/replacing files on Windows."""
    handles = []
    try:
        if os.name == "nt":
            import win32con
            import win32file

            directories = list(reversed([p for p in path.parents if p == root or p.is_relative_to(root)]))
            for directory in directories:
                handles.append(
                    win32file.CreateFile(
                        str(directory),
                        0x0080,
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                        None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS | 0x00200000,
                        None,
                    )
                )
        yield
    finally:
        for handle in reversed(handles):
            handle.Close()


class BuiltinTools:
    def __init__(self, registry, store, artifacts, policy, processes):
        self.registry, self.store, self.artifacts, self.policy, self.processes = (
            registry,
            store,
            artifacts,
            policy,
            processes,
        )
        self.scheduler = self.context = self.skills = None
        self.register()

    def register(self):
        def add(
            name,
            description,
            props,
            required,
            executor,
            capabilities,
            effect="read",
            retry="safe",
            timeout=60,
        ):
            self.registry.register(
                ToolSpec(
                    id=name,
                    description=description,
                    input_schema=schema(props, required),
                    required_capabilities=capabilities,
                    effect=effect,
                    retry_class=retry,
                    timeout=timeout,
                ),
                executor,
            )

        add("list_files", "列出工作区内目录，最多 500 项", {"path": PATH}, [], self.list_files, ["file_read"])
        add(
            "stat_file",
            "读取文件信息和用于安全写入的 SHA256",
            {"path": PATH},
            ["path"],
            self.stat_file,
            ["file_read"],
        )
        add(
            "read_file",
            "有界读取文件并返回原文产物与版本 hash；offset/limit 是字节，按行读取用 start_line/line_count，不能混用",
            {
                "path": PATH,
                "offset": {"type": "integer", "minimum": 0, "description": "从 0 开始的字节偏移，不是行号"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1048576,
                    "description": "最多读取的字节数",
                },
                "start_line": {"type": "integer", "minimum": 1, "description": "从 1 开始的行号"},
                "line_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "按行读取时最多返回的完整行数",
                },
            },
            ["path"],
            self.read_file,
            ["file_read"],
        )
        add(
            "search_files",
            "递归搜索工作区文本；返回匹配项与 complete、stop_reason，完整匹配行无需再次读取核验",
            {
                "path": PATH,
                "pattern": TEXT,
                "glob": {"type": "string", "description": "仅匹配文件名的 glob，默认 *"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 500},
            },
            ["pattern"],
            self.search,
            ["file_read"],
        )
        add(
            "write_file",
            "原子写入 UTF-8 文本，必须使用先读取的 expected_hash；新文件用 null",
            {"path": PATH, "content": TEXT, "expected_hash": {"type": ["string", "null"]}},
            ["path", "content", "expected_hash"],
            self.write,
            ["file_write"],
            "workspace_write",
            "manual_reconcile",
        )
        add(
            "apply_patch",
            "按唯一 old_text 替换为 new_text，强制 expected_hash 防止覆盖并发修改",
            {"path": PATH, "old_text": TEXT, "new_text": TEXT, "expected_hash": TEXT},
            ["path", "old_text", "new_text", "expected_hash"],
            self.patch,
            ["file_write"],
            "workspace_write",
            "manual_reconcile",
        )
        add(
            "exec",
            "经审批在工作区执行参数数组；可信本机执行，不是沙箱",
            {
                "argv": {"type": "array", "items": TEXT, "minItems": 1},
                "cwd": PATH,
                "timeout": {"type": "number", "minimum": 0.1, "maximum": 3600},
            },
            ["argv"],
            self.exec,
            ["process"],
            "process",
            "manual_reconcile",
            3610,
        )
        add(
            "poll",
            "按字节偏移读取已登记命令会话结果",
            {"session_id": TEXT, "offset": INTEGER},
            ["session_id"],
            self.poll,
            ["process"],
        )
        add(
            "fetch",
            "抓取公开 HTTP(S) 文本，重新校验重定向并固定连接 IP",
            {"url": TEXT},
            ["url"],
            self.fetch,
            ["network"],
            timeout=40,
        )
        add(
            "search_web",
            "搜索公开网页并返回来源链接；结果作为不可信外部材料",
            {"query": TEXT},
            ["query"],
            self.search_web,
            ["network"],
            timeout=40,
        )
        add(
            "plan_update",
            "保存本任务的步骤计划；不改变任务状态或权限",
            {
                "steps": {
                    "type": "array",
                    "items": schema(
                        {"text": TEXT, "status": {"enum": ["pending", "in_progress", "completed"]}},
                        ["text", "status"],
                    ),
                    "maxItems": 50,
                }
            },
            ["steps"],
            self.plan_update,
            ["input"],
        )
        add(
            "cancel_command",
            "停止本任务内已登记的命令会话；已发生副作用不会回滚",
            {"session_id": TEXT},
            ["session_id"],
            self.cancel_command,
            ["process"],
        )
        add(
            "request_input",
            "请求用户补充信息，持久暂停并释放执行槽",
            {"prompt": TEXT},
            ["prompt"],
            self.request_input,
            ["input"],
        )
        add(
            "request_approval",
            "请求用户确认指定说明；确认仅约束本交互，不给后续工具扩权",
            {"prompt": TEXT, "target": TEXT, "reason": TEXT},
            ["prompt", "target", "reason"],
            self.request_approval,
            ["input"],
        )
        add(
            "artifact_create",
            "保存 UTF-8 文本产物",
            {"name": TEXT, "content": TEXT, "mime_type": TEXT},
            ["name", "content"],
            self.artifact_create,
            ["artifact"],
        )
        add(
            "artifact_read",
            "按范围读取获授权的产物",
            {
                "artifact_id": TEXT,
                "offset": INTEGER,
                "limit": {"type": "integer", "maximum": 65536, "minimum": 1},
            },
            ["artifact_id"],
            self.artifact_read,
            ["artifact"],
        )
        add("artifact_list", "列出当前任务可见产物及预览引用", {}, [], self.artifact_list, ["artifact"])
        add(
            "artifact_preview",
            "读取产物安全预览摘要与受控引用",
            {"artifact_id": TEXT},
            ["artifact_id"],
            self.artifact_read,
            ["artifact"],
        )
        add(
            "archive_search",
            "检索当前分支的原始消息档案，返回出处",
            {"query": TEXT},
            ["query"],
            self.archive_search,
            ["file_read"],
        )
        add(
            "memory_search",
            "在授权范围内检索已接受且未过期的记忆",
            {"query": TEXT},
            ["query"],
            self.memory_search,
            ["memory"],
        )
        add(
            "delegate",
            "委派有边界的子任务，默认独立上下文并共享根预算",
            {"goal": TEXT, "completion_criteria": TEXT, "capabilities": {"type": "array", "items": TEXT}},
            ["goal", "completion_criteria"],
            self.delegate,
            ["delegate"],
        )
        add(
            "inspect_child",
            "查看当前任务的子任务状态和结果",
            {"child_run_id": TEXT},
            ["child_run_id"],
            self.inspect_child,
            ["delegate"],
        )
        add(
            "wait_children",
            "持久等待子任务完成并释放 worker 执行槽",
            {
                "child_run_ids": {"type": "array", "items": TEXT, "minItems": 1},
                "mode": {"enum": ["all", "any"]},
            },
            ["child_run_ids"],
            self.wait_children,
            ["delegate"],
        )
        add(
            "memory_propose",
            "提议保存带出处记忆，待用户批准后才可检索",
            {"content": TEXT, "source_refs": {"type": "array", "items": {"type": "string"}}},
            ["content", "source_refs"],
            self.memory_propose,
            ["memory"],
        )
        add(
            "skill_activate",
            "按固定快照显式加载获准的 Skill",
            {"skill_id": TEXT, "content_hash": TEXT},
            ["skill_id"],
            self.skill_activate,
            ["skills"],
        )
        add(
            "skill_read",
            "读取已激活 Skill 的资源文件",
            {"activation_id": TEXT, "resource": TEXT},
            ["activation_id", "resource"],
            self.skill_read,
            ["skills"],
        )

    def _root(self, ctx, path=None):
        roots = [Path(value).resolve() for value in self.store.workspace(ctx.workspace_id, ctx.owner_id)["roots"]]
        if path is None:
            return roots[0]
        return max((root for root in roots if path.is_relative_to(root)), key=lambda root: len(root.parts))

    def _display_path(self, ctx, path):
        root = self._root(ctx)
        return str(path.relative_to(root)) if path.is_relative_to(root) else str(path)

    @staticmethod
    def _hash(path):
        if not path.exists():
            return None
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    async def list_files(self, ctx, op, args):
        path = self.policy.path(ctx, args.get("path", "."))
        if not path.is_dir():
            raise HarnessError("NOT_DIRECTORY", "目标不是目录", 422)
        items = []
        for entry in sorted(path.iterdir()):
            if len(items) >= 500:
                break
            try:
                safe = self.policy.path(ctx, str(entry))
                items.append(
                    dict(
                        name=entry.name,
                        type="directory" if safe.is_dir() else "file",
                        size=safe.stat().st_size,
                    )
                )
            except (HarnessError, OSError):
                continue
        return dict(
            summary=f"{len(items)} 项",
            structured_data=dict(path=self._display_path(ctx, path), items=items),
            truncated=len(items) >= 500,
        )

    async def stat_file(self, ctx, op, args):
        path = self.policy.path(ctx, args["path"])
        return dict(
            summary="文件信息",
            structured_data=dict(
                path=args["path"],
                exists=path.exists(),
                is_directory=path.is_dir(),
                size=path.stat().st_size if path.exists() else 0,
                sha256=self._hash(path) if path.is_file() else None,
            ),
        )

    async def read_file(self, ctx, op, args):
        path = self.policy.path(ctx, args["path"])
        if not path.is_file():
            raise HarnessError("FILE_NOT_FOUND", "文件不存在", 404)
        line_mode = "start_line" in args or "line_count" in args
        if line_mode and ("offset" in args or "limit" in args):
            raise HarnessError("INVALID_READ_RANGE", "字节范围与行范围不能混用", 422)
        if "line_count" in args and "start_line" not in args:
            raise HarnessError("INVALID_READ_RANGE", "按行读取必须提供 start_line", 422)
        with directory_guards(path, self._root(ctx, path)):
            self.policy.path(ctx, args["path"])
            if line_mode:
                start_line, line_count = args.get("start_line", 1), args.get("line_count", 200)
                selected = []
                has_more = False
                with path.open("r", encoding="utf-8", errors="replace") as source:
                    for line_no, line in enumerate(source, 1):
                        if line_no < start_line:
                            continue
                        if len(selected) >= line_count:
                            has_more = True
                            break
                        selected.append(line)
                text = "".join(selected)
                if len(text.encode("utf-8")) > 1048576:
                    raise HarnessError("READ_LIMIT", "所选完整行超过 1 MiB，请缩小 line_count", 422)
                range_data = dict(
                    mode="lines",
                    start_line=start_line,
                    next_line=start_line + len(selected),
                )
                truncated = has_more
            else:
                offset, limit = args.get("offset", 0), args.get("limit", 65536)
                with path.open("rb") as source:
                    source.seek(offset)
                    chunk = source.read(limit)
                text = chunk.decode("utf-8", errors="replace")
                range_data = dict(mode="bytes", offset=offset, next_offset=offset + len(chunk))
                truncated = offset + len(chunk) < path.stat().st_size
            digest = self._hash(path)
            refs = []
            if path.stat().st_size <= self.artifacts.max_bytes:
                with path.open("rb") as source:
                    refs = [
                        self.artifacts.put_stream(
                            ctx.owner_id,
                            ctx.workspace_id,
                            iter(lambda: source.read(65536), b""),
                            "text/plain",
                            op["id"],
                            path.name,
                        )
                    ]
                self.store.reference_artifact(refs[0].artifact_id, "run", ctx.run_id)
        return dict(
            summary=text[:16000],
            structured_data=dict(path=args["path"], content=text, sha256=digest, **range_data),
            artifact_refs=refs,
            truncated=truncated,
        )

    async def search(self, ctx, op, args):
        root = self.policy.path(ctx, args.get("path", "."))
        max_results = args.get("max_results", 100)
        results, files_examined = [], 0
        stop_reason = None

        def candidates():
            if root.is_file():
                yield root
                return
            for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                current_path = Path(current)
                kept = []
                for name in sorted(directories):
                    child = current_path / name
                    if name in SEARCH_EXCLUDED_DIRECTORIES:
                        continue
                    try:
                        self.policy.path(ctx, str(child))
                    except (HarnessError, OSError):
                        continue
                    kept.append(name)
                directories[:] = kept
                for name in sorted(files):
                    yield current_path / name

        for path in candidates():
            if not fnmatch.fnmatch(path.name, args.get("glob", "*")):
                continue
            try:
                safe = self.policy.path(ctx, str(path))
                if not safe.is_file():
                    continue
                if safe.stat().st_size > SEARCH_FILE_MAX_BYTES:
                    continue
                if files_examined >= SEARCH_FILE_LIMIT:
                    stop_reason = "file_limit"
                    break
                files_examined += 1
                with safe.open("r", encoding="utf-8", errors="replace") as source:
                    for line_no, line in enumerate(source, 1):
                        if args["pattern"].casefold() not in line.casefold():
                            continue
                        if len(results) >= max_results:
                            stop_reason = "result_limit"
                            break
                        text = line.rstrip("\r\n")
                        results.append(
                            dict(
                                path=self._display_path(ctx, safe),
                                line=line_no,
                                text=text[:SEARCH_LINE_MAX_CHARS],
                                text_truncated=len(text) > SEARCH_LINE_MAX_CHARS,
                            )
                        )
                if stop_reason:
                    break
            except (OSError, HarnessError):
                continue
        complete = stop_reason is None
        return dict(
            summary=f"找到 {len(results)} 条匹配；" + ("扫描完成" if complete else f"提前停止: {stop_reason}"),
            structured_data=dict(
                matches=results,
                complete=complete,
                stop_reason=stop_reason,
            ),
            truncated=not complete,
        )

    async def write(self, ctx, op, args):
        path = self.policy.path(ctx, args["path"], write=True)
        if not path.parent.is_dir():
            raise HarnessError("PARENT_MISSING", "父目录不存在；请先选择现有目录", 422)
        raw = args["content"].encode("utf-8")
        if len(raw) > 16 * 1024 * 1024:
            raise HarnessError("FILE_TOO_LARGE", "写入超过 16MB 限制", 413)
        with directory_guards(path, self._root(ctx, path)):
            self.policy.path(ctx, args["path"], write=True)
            before = self._hash(path)
            if before != args["expected_hash"]:
                raise HarnessError("FILE_VERSION_CONFLICT", "文件版本已变化，必须重新读取后修改")
            fd, name = tempfile.mkstemp(prefix=".harness-", dir=path.parent)
            temporary = Path(name)
            after = hashlib.sha256(raw).hexdigest()
            try:
                with os.fdopen(fd, "wb") as target:
                    target.write(raw)
                    target.flush()
                    os.fsync(target.fileno())
                current = self.store.operation(op["id"])
                self.store.update_operation(
                    op["id"],
                    current["revision"],
                    dict(
                        metadata={
                            **current["metadata"],
                            "path": str(path),
                            "before_hash": before,
                            "after_hash": after,
                            "temp_path": str(temporary),
                        }
                    ),
                    ctx,
                )
                if self._hash(path) != before:
                    raise HarnessError("FILE_VERSION_CONFLICT", "提交前文件被其他程序修改")
                self.policy.path(ctx, args["path"], write=True)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        ref = self.artifacts.writer(ctx, raw, "text/plain", op["id"])
        return dict(
            summary=f"已写入 {args['path']}",
            structured_data=dict(path=args["path"], sha256=after, size=len(raw)),
            artifact_refs=[ref],
        )

    async def patch(self, ctx, op, args):
        path = self.policy.path(ctx, args["path"], write=True)
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise HarnessError("PATCH_TARGET", "补丁目标不存在或超过大小限制", 422)
        original = path.read_text(encoding="utf-8")
        if not args["old_text"] or original.count(args["old_text"]) != 1:
            raise HarnessError("PATCH_AMBIGUOUS", "补丁旧文本必须恰好匹配一次", 422)
        return await self.write(
            ctx,
            op,
            dict(
                path=args["path"],
                expected_hash=args["expected_hash"],
                content=original.replace(args["old_text"], args["new_text"], 1),
            ),
        )

    async def exec(self, ctx, op, args):
        cwd = self.policy.path(ctx, args.get("cwd", "."))
        return await self.processes.execute(ctx, op, args, cwd, 1024 * 1024)

    async def poll(self, ctx, op, args):
        result = self.processes.poll(ctx, args["session_id"], args.get("offset", 0))
        return dict(summary=result["output"], structured_data=result)

    async def fetch(self, ctx, op, args):
        def retrieve():
            url = args["url"]
            for _ in range(6):
                addresses = self.policy.endpoint(url, ctx, purpose="fetch")
                parsed = urlsplit(url)
                host, port = parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
                if parsed.scheme == "https":
                    pool = urllib3.HTTPSConnectionPool(
                        addresses[0],
                        port=port,
                        server_hostname=host,
                        assert_hostname=host,
                        cert_reqs="CERT_REQUIRED",
                        timeout=urllib3.Timeout(connect=5, read=10),
                        retries=False,
                    )
                else:
                    pool = urllib3.HTTPConnectionPool(
                        addresses[0], port=port, timeout=urllib3.Timeout(connect=5, read=10), retries=False
                    )
                try:
                    host_header = f"{host}:{port}" if parsed.port else host
                    target = parsed.path or "/"
                    if parsed.query:
                        target += "?" + parsed.query
                    response = pool.urlopen(
                        "GET",
                        target,
                        headers={"Host": host_header, "User-Agent": "LocalHarness/0.1"},
                        redirect=False,
                        preload_content=False,
                    )
                    try:
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location:
                                raise HarnessError("INVALID_REDIRECT", "响应缺少重定向目标", 422)
                            url = urljoin(url, location)
                            continue
                        mime = response.headers.get("Content-Type", "application/octet-stream").split(";")[0]
                        if not (mime.startswith("text/") or mime in {"application/json", "application/xml"}):
                            raise HarnessError("FETCH_TYPE", "仅支持文本网络响应", 422)
                        raw = response.read(1024 * 1024 + 1)
                        truncated = len(raw) > 1024 * 1024
                        return raw[: 1024 * 1024], mime, url, response.status, truncated
                    finally:
                        response.close()
                finally:
                    pool.close()
            raise HarnessError("REDIRECT_LIMIT", "重定向次数超过限制", 422)

        raw, mime, url, status, truncated = await asyncio.to_thread(retrieve)
        ref = self.artifacts.writer(ctx, raw, mime, op["id"])
        return dict(
            summary=raw[:16000].decode("utf-8", errors="replace"),
            structured_data=dict(
                source_url=url,
                http_status=status,
                trust="untrusted_external",
                text=raw[:32000].decode("utf-8", errors="replace"),
            ),
            artifact_refs=[ref],
            truncated=truncated,
        )

    async def request_input(self, ctx, op, args):
        interaction = self.store.prepare_interaction(
            ctx,
            "input:" + op["id"],
            "user_input",
            dict(prompt=args["prompt"], response_schema=schema({"text": TEXT}, ["text"])),
            after_seconds(3600),
        )
        if interaction["status"] in {"pending_binding", "open"}:
            return ToolWait(kind="user", interaction_id=interaction["id"], operation_id=op["id"])
        resolution = interaction.get("resolution") or {}
        return dict(
            summary="用户补充信息"
            if resolution.get("outcome") == "answered"
            else "交互已过期，未获得补充信息",
            structured_data=resolution,
        )

    async def request_approval(self, ctx, op, args):
        return dict(
            summary="此说明已获用户确认；后续操作仍需各自权限校验",
            structured_data=dict(
                target=args["target"], reason=args["reason"], authorization_scope="this_interaction_only"
            ),
        )

    async def search_web(self, ctx, op, args):
        import xml.etree.ElementTree as ET

        response = await self.fetch(
            ctx,
            op,
            {"url": "https://www.bing.com/search?" + urlencode({"q": args["query"], "format": "rss"})},
        )
        text = response["structured_data"]["text"]
        try:
            root = ET.fromstring(text)
            results = [
                {
                    "title": item.findtext("title"),
                    "url": item.findtext("link"),
                    "snippet": item.findtext("description"),
                    "trust": "untrusted_external",
                }
                for item in root.findall(".//item")[:20]
            ]
        except ET.ParseError:
            raise HarnessError("SEARCH_UNAVAILABLE", "搜索服务未返回可解析结果；可使用已配置的搜索 MCP", 502)
        response.update(
            summary=f"搜索返回 {len(results)} 条结果",
            structured_data=dict(
                query=args["query"], results=results, source_url=response["structured_data"]["source_url"]
            ),
        )
        return response

    async def plan_update(self, ctx, op, args):
        self.store.assert_fence(ctx)
        old = self.store.get("plans", ctx.run_id) or {"revision": 0}
        plan = dict(plan_id=ctx.run_id, run_id=ctx.run_id, steps=args["steps"], updated_at=utc_now(),
                    revision=old["revision"] + 1, content_hash=content_hash(args["steps"]))
        self.store.put("plans", ctx.run_id, plan)
        return dict(summary="计划已更新", structured_data=plan)

    async def cancel_command(self, ctx, op, args):
        record = self.processes.poll(ctx, args["session_id"])
        active = self.processes.active.get(record["operation_id"])
        if active:
            active["cancel"].set()
        return dict(
            summary="已请求停止命令" if active else "命令已经结束",
            structured_data=dict(
                session_id=args["session_id"], stop_requested=bool(active), status=record["status"]
            ),
        )

    async def artifact_list(self, ctx, op, args):
        rows = self.store.run_artifacts(ctx.run_id)
        refs = [self.artifacts.ref(row) for row in rows]
        return dict(
            summary=f"{len(refs)} 项产物",
            structured_data=dict(
                items=[
                    dict(artifact_ref=ref.model_dump(mode="json"), display_name=row["display_name"])
                    for ref, row in zip(refs, rows)
                ]
            ),
            artifact_refs=refs,
        )

    async def archive_search(self, ctx, op, args):
        matches = []
        for message in self.store.messages(ctx.branch_id):
            text = "\n".join(p.get("text", "") for p in message["content_parts"] if p["type"] == "text")
            if args["query"].casefold() in text.casefold():
                matches.append(
                    dict(
                        message_id=message["message_id"],
                        source_ref="message:" + message["message_id"],
                        text=text[:3000],
                    )
                )
                if len(matches) >= 30:
                    break
        return dict(summary=f"找到 {len(matches)} 条档案", structured_data=dict(matches=matches))

    async def memory_search(self, ctx, op, args):
        records = self.context.retrieve_memory(ctx, args["query"])
        return dict(
            summary=f"找到 {len(records)} 条记忆",
            structured_data=dict(
                items=[r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in records]
            ),
        )

    async def artifact_create(self, ctx, op, args):
        ref = self.artifacts.put_bytes(
            ctx.owner_id,
            ctx.workspace_id,
            args["content"].encode(),
            args.get("mime_type", "text/plain"),
            op["id"],
            args["name"],
        )
        self.store.reference_artifact(ref.artifact_id, "run", ctx.run_id)
        return dict(
            summary=f"已保存产物 {args['name']}",
            structured_data=dict(artifact_ref=ref.model_dump(mode="json")),
            artifact_refs=[ref],
        )

    async def artifact_read(self, ctx, op, args):
        row = self.store.artifact(args["artifact_id"], ctx.owner_id)
        if row["workspace_id"] != ctx.workspace_id:
            raise HarnessError("ARTIFACT_SCOPE", "产物不属于当前工作区", 403)
        raw = self.artifacts.read_range(
            args["artifact_id"], ctx.owner_id, args.get("offset", 0), args.get("limit", 65536)
        )
        return dict(
            summary=raw[:16000].decode("utf-8", errors="replace"),
            structured_data=dict(
                content=raw.decode("utf-8", errors="replace"), next_offset=args.get("offset", 0) + len(raw)
            ),
        )

    async def delegate(self, ctx, op, args):
        child = self.scheduler.delegate(
            ctx,
            OperationKey(run_id=ctx.run_id, message_id=op["message_id"], tool_call_id=op["tool_call_id"]),
            args,
        )
        return dict(
            summary="子任务已排队", structured_data=dict(child_run_id=child["id"], status=child["status"])
        )

    async def inspect_child(self, ctx, op, args):
        children = {x["id"]: x for x in self.store.children(ctx.run_id)}
        if args["child_run_id"] not in children:
            raise HarnessError("CHILD_SCOPE", "子任务不属于当前父任务", 403)
        child = children[args["child_run_id"]]
        return dict(
            summary=child["status"],
            structured_data=dict(
                run_id=child["id"],
                status=child["status"],
                result_ref=child["result_ref"],
                error=child["error"],
            ),
        )

    async def wait_children(self, ctx, op, args):
        wait = self.scheduler.prepare_wait(ctx, op["id"], args["child_run_ids"], args.get("mode", "all"))
        if wait["resolution"]:
            return dict(summary="子任务等待已满足", structured_data=wait["resolution"])
        return ToolWait(kind="children", wait_id=wait["id"], operation_id=op["id"])

    async def memory_propose(self, ctx, op, args):
        if self.context is None:
            raise HarnessError("MEMORY_UNAVAILABLE", "记忆服务尚未连接", 503)
        result = self.context.propose_memory(ctx, args["content"], args["source_refs"])
        return dict(
            summary="记忆提议已保存，待用户审阅",
            structured_data=result.model_dump(mode="json") if hasattr(result, "model_dump") else result,
        )

    async def skill_activate(self, ctx, op, args):
        if self.skills is None:
            raise HarnessError("SKILLS_UNAVAILABLE", "Skill 服务尚未连接", 503)
        snapshot = self.store.get("snapshots", ctx.config_snapshot_id)
        allowed = [x["skill_id"] for x in snapshot.get("skills", [])]
        activated = self.skills.activate(
            args["skill_id"], ctx, content_hash=args.get("content_hash"), allowlist=allowed
        )
        return dict(summary=activated.body, structured_data=activated.model_dump(mode="json"))

    async def skill_read(self, ctx, op, args):
        resource = self.skills.read_resource(args["activation_id"], args["resource"], ctx)
        raw = resource.content
        metadata = resource.model_dump(mode="json", exclude={"content"})
        metadata["size_bytes"] = len(raw)
        text_mime = resource.mime_type.startswith("text/") or resource.mime_type in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/x-yaml",
        }
        text = None
        if text_mime:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                pass
        if text is not None and len(raw) <= 16384:
            return dict(summary=text, structured_data={**metadata, "content": text})
        ref = self.artifacts.writer(
            ctx, raw, resource.mime_type, "skill:" + resource.snapshot_ref + ":" + resource.content_hash
        )
        return dict(
            summary=f"Skill 资源已保存：{resource.mime_type}，{len(raw)} 字节",
            structured_data={**metadata, "artifact_ref": ref.model_dump(mode="json")},
            artifact_refs=[ref],
        )
