"""Run frozen official TB2.1 tasks through the installed Mi Harness runtime.

Run from an isolated wheel installation, not the source checkout. Model credentials
stay in the host OS vault; Docker receives neither credentials nor host mounts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

TASKS = (
    "build-cython-ext", "cancel-async-tasks", "configure-git-webserver",
    "count-dataset-tokens", "fix-git", "large-scale-text-editing",
    "log-summary-date-ranges", "multi-source-data-merger",
    "openssl-selfsigned-cert", "sqlite-db-truncate",
)
COMMIT = "7131e4375048a0e408a8fb404b5f499d726b695b"


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class Ledger:
    """Integer micro-yuan; unknown responses keep the full reservation."""
    def __init__(self, path, budget_cny=15):
        self.budget_cny = budget_cny
        self.limit = round(budget_cny * 1_000_000)
        self.budget_exhausted = False
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, task TEXT, reserved INTEGER, charged INTEGER, detail TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS ledger_config (id TEXT PRIMARY KEY, value INTEGER)")
        existing = self.db.execute("SELECT value FROM ledger_config WHERE id='limit'").fetchone()
        if existing and existing[0] != self.limit:
            raise RuntimeError("Cannot change an existing run's budget")
        self.db.execute("INSERT OR IGNORE INTO ledger_config VALUES ('limit',?)", (self.limit,))
        self.db.commit()

    def spent(self):
        return self.db.execute("SELECT COALESCE(SUM(COALESCE(charged,reserved)),0) FROM calls").fetchone()[0]

    def reserve(self, task, body):
        if "max_completion_tokens" in body or not isinstance(body.get("max_tokens"), int) or body["max_tokens"] <= 0:
            raise RuntimeError("DeepSeek requires an explicit positive max_tokens wire limit")
        # UTF-8 bytes upper-bound text token count, with room for protocol framing.
        n = len(json.dumps(body, ensure_ascii=False).encode()) + 8192
        ceiling = n * 2 + body.get("max_completion_tokens", body.get("max_tokens", 16384)) * 8
        identity = str(uuid4())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.spent() + ceiling > self.limit:
                self.budget_exhausted = True
                raise RuntimeError("SMOKE_BUDGET_EXHAUSTED")
            self.db.execute("INSERT INTO calls VALUES (?,?,?,NULL,?)", (identity, task, ceiling, json.dumps({"model":body["model"],"reasoning_effort":body["reasoning_effort"],"thinking":body["thinking"],"max_tokens":body["max_tokens"],"at":datetime.now(UTC).isoformat()})))
        return identity

    def reconcile_legacy_unknown(self):
        """Retain the old input allowance and reserve the documented provider maximum."""
        with self.db:
            for identity, reserved, detail in self.db.execute("SELECT id,reserved,detail FROM calls WHERE charged IS NULL").fetchall():
                data = json.loads(detail)
                if "max_tokens" in data or "legacy_reservation_repair" in data:
                    continue
                corrected = reserved + (393216 - 16384) * 8
                data["legacy_reservation_repair"] = {"old_micro_cny": reserved, "new_micro_cny": corrected, "output_ceiling": 393216, "reason": "SDK sent unsupported max_completion_tokens; interrupted request has no usage"}
                self.db.execute("UPDATE calls SET reserved=?,detail=? WHERE id=?", (corrected, json.dumps(data), identity))

    def settle(self, identity, usage):
        if not usage or "prompt_tokens" not in usage or "completion_tokens" not in usage:
            return
        # Charge the accounting ceiling at peak rates, even when real billing is off-peak.
        hit = usage.get("prompt_cache_hit_tokens", usage.get("prompt_tokens_details", {}).get("cached_tokens", 0))
        miss = usage["prompt_tokens"] - hit
        charged = (hit * 4 + 99) // 100 + miss * 2 + usage["completion_tokens"] * 8
        with self.db:
            row = self.db.execute("SELECT detail FROM calls WHERE id=?", (identity,)).fetchone()
            detail = json.loads(row[0])
            detail["usage"] = usage
            self.db.execute("UPDATE calls SET charged=?,detail=? WHERE id=?", (charged,json.dumps(detail),identity))

    def report(self):
        rows = self.db.execute("SELECT id,task,reserved,charged,detail FROM calls").fetchall()
        calls = [dict(id=r[0],task=r[1],reserved_cny=r[2]/1e6,charged_peak_cny=None if r[3] is None else r[3]/1e6,**json.loads(r[4])) for r in rows]
        actual = 0
        for call in calls:
            local = datetime.fromisoformat(call["at"]) + timedelta(hours=8)
            peak = local.weekday()<5 and (9<=local.hour<12 or 14<=local.hour<18)
            if call["charged_peak_cny"] is not None:
                call["estimated_cny"] = call["charged_peak_cny"]*(1 if peak else 0.5)
                actual += call["estimated_cny"]
        return {"budget_cny":self.budget_cny,"conservative_spend_cny":self.spent()/1e6,"known_usage_estimated_cny":actual,"unknown_calls":sum(r[3] is None for r in rows),"calls":calls}


async def command(*args, timeout=120, stdin=None):
    proc = await asyncio.create_subprocess_exec(*map(str,args), stdin=asyncio.subprocess.PIPE if stdin is not None else None, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin),timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8",errors="replace")


class ContainerTools:
    def __init__(self, container, store, folder, artifacts):
        self.container,self.store,self.folder,self.artifacts = container,store,folder,artifacts

    async def execute(self, ctx, key, name, args):
        from harness.tools.contracts import ToolResult
        if name == "artifact_read":
            from harness.core import HarnessError
            row = self.store.artifact(args["artifact_id"],ctx.owner_id)
            if row["workspace_id"] != ctx.workspace_id:
                raise HarnessError("artifact_not_accessible","Artifact is outside this workspace",403)
            offset = max(0,int(args.get("offset",0)))
            length = min(max(1,int(args.get("length",4000))),4000)
            raw = self.artifacts.read_range(args["artifact_id"],ctx.owner_id,offset,length)
            return ToolResult(operation_id=key.tool_call_id,status="succeeded",summary=raw.decode("utf-8",errors="replace"),truncated=offset+len(raw)<row["size_bytes"])
        prior = self.store.get("smoke_operations", key.tool_call_id)
        if prior:
            if prior.get("state") == "running":
                raise RuntimeError("SMOKE_UNKNOWN_TOOL_EFFECT")
            return ToolResult.model_validate(prior["result"])
        self.store.put("smoke_operations",key.tool_call_id,{"state":"running","args":args})
        seconds = min(max(int(args.get("timeout",60)),1),180)
        try:
            code,output = await command("docker","exec",self.container,"timeout","-k","3",str(seconds),"bash","-lc",args["command"],timeout=seconds+15)
        except asyncio.CancelledError:
            # Stop the outstanding harness-owned timeout group before scoring.
            await asyncio.shield(command("docker","exec",self.container,"pkill","-TERM","-f","^timeout -k 3 ",timeout=15))
            raise
        (self.folder / f"tool-{key.tool_call_id}.txt").write_text(output,encoding="utf-8")
        result = ToolResult(operation_id=key.tool_call_id,status="succeeded" if code==0 else "failed",summary=output[:24000] or "(no output)",exit_code=code,truncated=len(output)>24000)
        self.store.put("smoke_operations",key.tool_call_id,{"state":"finished","args":args,"result":result.model_dump()})
        print(f"tool {ctx.run_id} exit={code} bytes={len(output)}",flush=True)
        return result


async def run_agent(task, container, folder, ledger, timeout):
    from harness.artifacts import ArtifactStore
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from harness.context import ContextService
    from harness.core import ExecutionContext
    from harness.model_gateway import ModelGateway, ModelProfile
    from harness.model_gateway.providers import build_provider_model
    from harness.platform.config import default_data_dir
    from harness.platform.credentials import CredentialVault
    from harness.runtime import AgentSpec, LangChainAgentRuntime
    from harness.storage import Store

    host = Store(default_data_dir()/"app.db")
    original = max((p for p in host.list("model_profiles") if p.get("model_id")=="deepseek-flash" and p.get("endpoint_ref")=="https://api.deepseek.com"),key=lambda p:p["revision"])
    credential = CredentialVault().get(original["credential_ref"],"model-"+original["profile_id"])
    profile = ModelProfile.model_validate(original)
    profile = profile.model_copy(update={"profile_id":"smoke-deepseek-high","revision":1,"limits":profile.limits.model_copy(update={"output_limit":16384}),"input_price_per_million":None,"output_price_per_million":None})
    save(folder/"model-profile.json",profile.model_dump(mode="json",exclude={"credential_ref"}))
    store = Store(folder/"runtime.db")
    workspace = store.create_workspace("smoke","Smoke artifacts",str(folder),{})
    artifacts = ArtifactStore(store,folder/"artifacts")

    async def request_hook(req):
        if req.url.host != "api.deepseek.com":
            raise RuntimeError("unexpected model destination")
        body = json.loads(req.content)
        assert body["model"]=="deepseek-flash" and body["reasoning_effort"]=="high"
        assert body["thinking"]=={"type":"enabled"} and not body.get("stream")
        assert body.get("max_tokens") == 16384 and "max_completion_tokens" not in body
        req.extensions["smoke_charge"] = ledger.reserve(folder.name,body)
        save(folder.parent/"budget.json",ledger.report())

    async def response_hook(resp):
        await resp.aread()
        identity = resp.request.extensions.get("smoke_charge")
        if identity:
            try:
                data = resp.json()
            except ValueError:
                return
            ledger.settle(identity,data.get("usage"))
            # Full response preserves tool/reasoning evidence but never auth headers.
            save(folder/f"response-{identity}.json",data)
            save(folder.parent/"budget.json",ledger.report())
            print(f"model {task.name} http={resp.status_code} budget_peak={ledger.spent()/1e6:.5f}/{ledger.budget_cny}",flush=True)

    def factory(p,endpoint,secret):
        model = build_provider_model(p,endpoint,secret)
        model.reasoning_effort = "high"
        model.extra_body = {"thinking":{"type":"enabled"}}
        model.http_async_client.event_hooks["request"].append(request_hook)
        model.http_async_client.event_hooks["response"].append(response_hook)
        return model

    def policy(ctx,p):
        assert p.model_id=="deepseek-flash" and p.endpoint_ref=="https://api.deepseek.com"

    async def event(kind,data):
        with (folder/"events.jsonl").open("a",encoding="utf-8") as f:
            f.write(json.dumps({"type":kind,"data":data},default=str)+"\n")

    gateway = ModelGateway(store,credential_accessor=lambda *a:credential,policy_check=policy,provider_factory=factory,max_attempts=1,event_sink=event)
    gateway.register(profile)
    ctx = ExecutionContext(owner_id="smoke",workspace_id="smoke",session_id=folder.name,branch_id=folder.name,run_id=folder.name,root_run_id="smoke-15cny",worker_attempt_id=folder.name,fencing_token=1,graph_thread_key=folder.name,input_revision=1,config_snapshot_id=folder.name,trace_id=folder.name,deadline_at=(datetime.now(UTC)+timedelta(seconds=timeout)).isoformat())
    ctx = ctx.model_copy(update={"workspace_id":workspace["id"],"root_run_id":f"smoke-{ledger.budget_cny}cny"})
    tool = {"name":"terminal","description":"Execute bash in the task container. Shell state does not persist; use explicit cd when needed. A command runs for timeout seconds (default 60, max 180).","input_schema":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"integer","minimum":1,"maximum":180}},"required":["command"],"additionalProperties":False}}
    artifact_tool = {"name":"artifact_read","description":"Read your stored large tool output by artifact_id, using byte offset and length (max 4000).","input_schema":{"type":"object","properties":{"artifact_id":{"type":"string"},"offset":{"type":"integer","minimum":0},"length":{"type":"integer","minimum":1,"maximum":4000}},"required":["artifact_id"],"additionalProperties":False}}
    snapshot = {"agent_spec":AgentSpec(model_policy={"profile_ref":profile.ref},tools=["terminal","artifact_read"],budget={"max_model_calls":100}).model_dump(),"tools":[tool,artifact_tool],"context_policy":{"output_reserve":16384}}
    store.put("snapshots",ctx.config_snapshot_id,snapshot)
    def artifact_reader(ctx,ref,limit):
        if ref.owner_id != ctx.owner_id or ref.workspace_id != ctx.workspace_id:
            raise RuntimeError("artifact outside smoke workspace")
        return artifacts.read_range(ref.artifact_id,ctx.owner_id,0,limit)
    composer = ContextService(store,model_gateway=gateway,policy={"output_reserve":16384},artifact_writer=artifacts.writer,artifact_reader=artifact_reader)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(folder/"checkpoints.db")) as saver:
            runtime = LangChainAgentRuntime(saver,gateway,composer,ContainerTools(container,store,folder,artifacts),repository=store,artifact_writer=artifacts.writer)
            try:
                result = await asyncio.wait_for(runtime.start(ctx,snapshot,(task/"instruction.md").read_text("utf-8")),timeout+10)
            except TimeoutError:
                checkpoint = None
                try:
                    checkpoint = (await runtime.get_checkpoint(ctx)).model_dump(mode="json")
                except Exception:
                    pass
                save(folder/"runtime-result.json",{"kind":"error","error":{"code":"deadline_exceeded","message":"Agent wall-clock limit reached"},"checkpoint_ref":checkpoint})
                return "error"
            save(folder/"runtime-result.json",result.model_dump(mode="json"))
            return result.kind
    finally:
        await gateway.aclose()


async def run(args):
    import portalocker
    import harness
    import importlib.metadata
    source,output = args.source.resolve(),args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    with portalocker.Lock(str(output/"run.lock"),timeout=0):
        rev = subprocess.check_output(["git","-C",str(source),"rev-parse","HEAD"],text=True).strip()
        assert rev==COMMIT
        manifest = {"commit":rev,"model":"deepseek-flash","reasoning_effort":"high","budget_cny":15,"tasks":list(TASKS),"files":{}}
        for name in TASKS:
            for file in (source/"tasks"/name).rglob("*"):
                if file.is_file():
                    manifest["files"][file.relative_to(source).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
        saved = output/"manifest.json"
        if saved.exists():
            assert json.loads(saved.read_text("utf-8"))==manifest
        else:
            save(saved,manifest)
        ledger = Ledger(output/"budget.sqlite")
        save(output/"budget.json",ledger.report())
        package = Path(harness.__file__).parent
        if "runtime-site" not in str(package):
            raise RuntimeError("Install the rebuilt wheel in runtime-site before running")
        save(output/"build-provenance.json",{"installed_package":str(package),"version":importlib.metadata.version("mi-harness"),"source_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),"installed_python_sha256":{str(p.relative_to(package)):hashlib.sha256(p.read_bytes()).hexdigest() for p in package.rglob("*.py")},"wheel_sha256":hashlib.sha256((Path(__file__).resolve().parents[1]/"dist/mi_harness-0.1.0-py3-none-any.whl").read_bytes()).hexdigest()})
        code,info = await command("docker","info","--format","{{.ServerVersion}}",timeout=30)
        if code:
            raise RuntimeError("Docker unavailable: "+info)
        for name in TASKS:
            if args.task and args.task!=name:
                continue
            previous = sorted(output.glob(name+"--*/result.json"))
            if previous and not args.retry:
                continue
            attempt = len(list(output.glob(name+"--*")))+1
            folder = output/f"{name}--{attempt:02}"
            folder.mkdir()
            task = source/"tasks"/name
            config = tomllib.loads((task/"task.toml").read_text("utf-8"))
            env = config["environment"]
            container = f"mi-smoke-{name}-{attempt}"
            runner_source = Path(__file__).read_bytes()
            (folder/"runner.py").write_bytes(runner_source)
            record = {"task":name,"attempt":attempt,"started":datetime.now(UTC).isoformat(),"container":container,"runner_sha256":hashlib.sha256(runner_source).hexdigest()}
            try:
                image = env["docker_image"]
                code,log = await command("docker","pull",image,timeout=900)
                (folder/"pull.log").write_text(log,encoding="utf-8")
                if code:
                    raise RuntimeError("image pull failed")
                code,log = await command("docker","run","-d","--name",container,"--label","mi-harness-smoke=true","--cpus",str(env["cpus"]),"--memory",f"{env['memory_mb']}m","--entrypoint","sleep",image,"infinity")
                if code:
                    raise RuntimeError("container start failed: "+log)
                _,digest = await command("docker","image","inspect",image,"--format","{{json .RepoDigests}}")
                record["image_digest"] = digest.strip()
                print("START "+name,flush=True)
                record["runtime_kind"] = await run_agent(task,container,folder,ledger,config["agent"]["timeout_sec"])
                # Mount no evaluator data during agent execution. Copy it only now.
                await command("docker","exec",container,"mkdir","-p","/tests","/logs/verifier")
                archive = subprocess.check_output(["git","-c","core.autocrlf=false","-C",str(source),"archive",COMMIT,f"tasks/{name}/tests"])
                code,log = await command("docker","exec","-i",container,"tar","-xf","-","--strip-components=3","-C","/tests",stdin=archive)
                if code:
                    raise RuntimeError("verifier copy failed: "+log)
                code,log = await command("docker","exec",container,"bash","/tests/test.sh",timeout=config["verifier"]["timeout_sec"]+30)
                (folder/"verifier.log").write_text(log,encoding="utf-8")
                record["verifier_exit"] = code
                rc,reward = await command("docker","exec",container,"cat","/logs/verifier/reward.txt")
                record["reward"] = float(reward.strip()) if rc==0 else None
                await command("docker","cp",container+":/logs/verifier",str(folder/"verifier"))
                record["status"] = "scored" if record["reward"] is not None and record["runtime_kind"]=="final" else "needs_diagnosis"
            except Exception as exc:
                record.update(status="error",error=f"{type(exc).__name__}: {exc}")
            finally:
                # Retain stopped task files for diagnosis; never delete images or user containers.
                await command("docker","stop","-t","3",container,timeout=20)
                record["finished"] = datetime.now(UTC).isoformat()
                save(folder/"result.json",record)
                save(output/"budget.json",ledger.report())
            print(json.dumps(record),flush=True)
            if record["status"]!="scored":
                break


if __name__=="__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--task",choices=TASKS)
    parser.add_argument("--retry",action="store_true")
    asyncio.run(run(parser.parse_args()))
