from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
from pathlib import Path

import psutil

from harness.core import HarnessError, new_id, utc_now


class ProcessExecutor:
    """Supervised process trees. Windows children start suspended before Job assignment."""

    def __init__(self, store, artifacts, data_dir: Path):
        self.store, self.artifacts, self.data_dir = store, artifacts, data_dir
        self.active: dict[str, dict] = {}
        (data_dir / "process-output").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def environment() -> dict[str, str]:
        names = ("SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC", "USERPROFILE")
        return {k: v for k, v in os.environ.items() if k.upper() in names}

    def _start(self, argv: list[str], cwd: Path):
        job = None
        if os.name == "nt":
            import win32api
            import win32con
            import win32job
            import win32process

            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=(0x00000004 | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            if job:
                try:
                    win32job.AssignProcessToJobObject(job, int(process._handle))
                    threads = psutil.Process(process.pid).threads()
                    if not threads:
                        raise RuntimeError("Suspended process has no initial thread")
                    handle = win32api.OpenThread(win32con.THREAD_SUSPEND_RESUME, False, threads[0].id)
                    try:
                        win32process.ResumeThread(handle)
                    finally:
                        handle.Close()
                except BaseException:
                    process.kill()
                    process.wait(timeout=5)
                    raise
            return process, job
        except BaseException:
            if job:
                job.Close()
            raise

    @staticmethod
    def _terminate(process, job):
        # Packaged Windows launchers can activate an application outside the
        # original job. Track and validate descendants, then attach/terminate
        # them explicitly rather than treating the initial PID as the tree.
        descendants = []
        try:
            descendants = [(p, p.create_time()) for p in psutil.Process(process.pid).children(recursive=True)]
        except psutil.Error:
            pass
        if job:
            import win32job

            win32job.TerminateJobObject(job, 1)
        else:
            import signal

            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child, started in reversed(descendants):
            try:
                if abs(child.create_time() - started) < 0.01 and child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs([p for p, _ in descendants], timeout=5)
        if alive:
            raise HarnessError("PROCESS_EXIT_UNKNOWN", "无法确认派生进程退出")
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise HarnessError("PROCESS_EXIT_UNKNOWN", "无法确认受控进程树退出") from exc

    async def execute(self, ctx, operation: dict, args: dict, cwd: Path, output_limit: int):
        argv = args["argv"]
        if not argv or not all(isinstance(x, str) and "\x00" not in x for x in argv):
            raise HarnessError("INVALID_COMMAND", "命令必须为非空参数数组", 422)
        process, job = self._start(argv, cwd)
        session_id = new_id()
        started = time.monotonic()
        record = dict(
            id=session_id,
            run_id=ctx.run_id,
            operation_id=operation["id"],
            pid=process.pid,
            process_start=psutil.Process(process.pid).create_time(),
            status="running",
            output_bytes=0,
            retained_bytes=0,
            started_at=utc_now(),
        )
        self.store.put("process_sessions", session_id, record)
        cancel = asyncio.Event()
        self.active[operation["id"]] = dict(
            process=process, job=job, cancel=cancel, run_id=ctx.run_id, session_id=session_id
        )
        fd, name = tempfile.mkstemp(dir=self.data_dir / "process-output")
        output_path = Path(name)

        def drain():
            total, retained = 0, 0
            with os.fdopen(fd, "wb") as target:
                while chunk := process.stdout.read(65536):
                    total += len(chunk)
                    keep = chunk[: max(0, output_limit - retained)]
                    target.write(keep)
                    retained += len(keep)
                target.flush()
                os.fsync(target.fileno())
            return total, retained

        reader = asyncio.create_task(asyncio.to_thread(drain))
        reason = None
        known_children = {}
        try:
            while process.poll() is None:
                if job:
                    try:
                        previous_count = len(known_children)
                        for child in psutil.Process(process.pid).children(recursive=True):
                            if child.pid in known_children:
                                continue
                            started_at = child.create_time()
                            # Reassigning an already active packaged application's
                            # OS-owned job can break its activation. Its identity
                            # belongs to the explicit tree-reaping path instead.
                            known_children[child.pid] = started_at
                        if len(known_children) != previous_count:
                            record["members"] = [
                                {"pid": pid, "process_start": birth} for pid, birth in known_children.items()
                            ]
                            self.store.put("process_sessions", session_id, record)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if cancel.is_set():
                    reason = "cancelled"
                    self._terminate(process, job)
                    break
                if time.monotonic() - started >= min(args.get("timeout", 60), 3600):
                    reason = "timeout"
                    self._terminate(process, job)
                    break
                await asyncio.sleep(0.03)
            # Closing a Windows Job also reaps descendants surviving their initial parent.
            if job:
                job.Close()
                job = None
            elif os.name != "nt":
                self._terminate(process, None)
            self._reap_members(known_children)
            total, retained = await asyncio.wait_for(reader, 5)
            with output_path.open("rb") as stream:
                ref = self.artifacts.put_stream(
                    ctx.owner_id,
                    ctx.workspace_id,
                    iter(lambda: stream.read(65536), b""),
                    "text/plain",
                    operation["id"],
                    "command-output.txt",
                )
            self.store.reference_artifact(ref.artifact_id, "run", ctx.run_id)
            excerpt = output_path.read_bytes()[:16000].decode("utf-8", errors="replace")
            status = (
                "cancelled"
                if reason == "cancelled"
                else "failed"
                if reason or process.returncode
                else "succeeded"
            )
            record.update(
                status=status,
                exit_code=process.returncode,
                output_bytes=total,
                retained_bytes=retained,
                artifact_ref=ref.model_dump(mode="json"),
                completed_at=utc_now(),
                reason=reason,
            )
            self.store.put("process_sessions", session_id, record)
            return dict(
                summary=excerpt or f"命令退出码 {process.returncode}",
                structured_data=dict(
                    session_id=session_id, output=excerpt, output_bytes=total, retained_bytes=retained
                ),
                status=status,
                exit_code=process.returncode,
                artifact_refs=[ref],
                truncated=total > retained,
                error_code="PROCESS_TIMEOUT" if reason == "timeout" else None,
            )
        except asyncio.CancelledError:
            self._terminate(process, job)
            await asyncio.shield(reader)
            record.update(status="cancelled", completed_at=utc_now())
            self.store.put("process_sessions", session_id, record)
            raise
        finally:
            if process.poll() is None:
                self._terminate(process, job)
            if job:
                job.Close()
            process.stdout.close()
            output_path.unlink(missing_ok=True)
            self.active.pop(operation["id"], None)

    @staticmethod
    def _reap_members(members):
        live = []
        for pid, birth in members.items():
            try:
                child = psutil.Process(int(pid))
                if abs(child.create_time() - birth) < 0.01 and child.is_running():
                    child.kill()
                    live.append(child)
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(live, timeout=5)
        if alive:
            raise HarnessError("PROCESS_EXIT_UNKNOWN", "已记录的派生进程尚未退出")

    def recover_run(self, run_id: str):
        for record in self.store.list("process_sessions"):
            if record.get("run_id") != run_id or record.get("status") != "running":
                continue
            members = {x["pid"]: x["process_start"] for x in record.get("members", [])}
            members[record["pid"]] = record["process_start"]
            self._reap_members(members)
            self.store.put(
                "process_sessions",
                record["id"],
                {**record, "status": "unknown", "reason": "worker_lost", "completed_at": utc_now()},
            )

    def cancel_run(self, run_id: str):
        for entry in self.active.values():
            if entry["run_id"] == run_id:
                entry["cancel"].set()

    def poll(self, ctx, session_id: str, offset: int = 0) -> dict:
        record = self.store.get("process_sessions", session_id)
        if not record or record["run_id"] != ctx.run_id:
            raise HarnessError("NOT_FOUND", "命令会话不存在", 404)
        content = b""
        if record.get("artifact_ref"):
            content = self.artifacts.read_range(
                record["artifact_ref"]["artifact_id"], ctx.owner_id, offset, 65536
            )
        return dict(
            **record, output=content.decode("utf-8", errors="replace"), next_offset=offset + len(content)
        )
