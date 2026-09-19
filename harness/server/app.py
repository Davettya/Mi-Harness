from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from harness.core import ArtifactRef, ErrorEnvelope, HarnessError, canonical_json
from harness.server.auth import AuthService
from harness.server.schemas import (
    AuthSessionView,
    BranchInput,
    CancelInput,
    ConfigInput,
    ConfigKind,
    ContextPinInput,
    InteractionResponse,
    McpDiagnoseInput,
    McpFileInput,
    McpFileSaveInput,
    MemoryPatch,
    MemoryReview,
    ModelDiscoveryInput,
    ModelSelectionInput,
    ModelSetupInput,
    ModelSetupSaveInput,
    ModelTestInput,
    Page,
    PairingInput,
    PluginLoadInput,
    PreferencesInput,
    ProjectInput,
    ProjectPatch,
    ProjectRemoveInput,
    ReconciliationInput,
    RunView,
    SessionInput,
    SessionPatch,
    SkillPatch,
    SkillRefreshInput,
    SteeringInput,
    SubmitReceipt,
    SubmitRunInput,
    WorkspaceInput,
)


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


class BodyLimitMiddleware:
    """Bound request bytes during receipt, including chunked uploads before multipart parsing."""

    def __init__(self, app, upload_limit: int):
        self.app, self.upload_limit = app, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.upload_limit + 65536 if scope["path"] == "/api/artifacts" else 2 * 1024 * 1024
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HarnessError("INPUT_TOO_LARGE", "输入超过配置大小上限", 413)
            return message

        await self.app(scope, bounded_receive, send)


def create_app(services: Any) -> FastAPI:
    """Routes only translate transport; services owns business mutation and authorization."""
    settings = services.settings
    auth = getattr(services, "auth", None) or AuthService(services.store)
    @asynccontextmanager
    async def lifespan(_app):
        opener, closer = getattr(services, "open", None), getattr(services, "close", None)
        if opener:
            await resolve(opener())
        try:
            yield
        finally:
            if closer:
                await resolve(closer())

    app = FastAPI(title="Mi Harness", version="0.1.0", lifespan=lifespan, responses={
        status: {"model": ErrorEnvelope} for status in (400, 401, 403, 404, 409, 410, 413, 422, 429, 503)
    })
    app.state.services, app.state.auth = services, auth
    app.add_middleware(BodyLimitMiddleware, upload_limit=getattr(services.artifacts, "max_bytes", 128 * 1024 * 1024))
    host, port = getattr(settings, "host", "127.0.0.1"), getattr(settings, "port", 8767)
    authority_host = f"[{host}]" if ":" in host else host
    allowed_hosts = set(getattr(settings, "allowed_hosts", None) or [f"{authority_host}:{port}", f"localhost:{port}"])
    allowed_origins = set(getattr(settings, "allowed_origins", None) or [f"http://{name}" for name in allowed_hosts])
    capabilities = getattr(services, "capabilities", {
        "branches": True, "steering": True, "memory": True, "children": True,
        "mcp_oauth": False, "mcp_elicitation": False,
    })

    @app.exception_handler(HarnessError)
    async def harness_error(_request: Request, error: HarnessError):
        return JSONResponse(error.envelope().model_dump(mode="json"), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError):
        locations = [".".join(str(x) for x in row["loc"]) for row in error.errors()]
        value = ErrorEnvelope(code="VALIDATION_ERROR", message="请求字段不符合契约: " + ", ".join(locations[:8]))
        return JSONResponse(value.model_dump(mode="json"), status_code=422)

    @app.exception_handler(ValidationError)
    async def domain_validation_error(_request: Request, error: ValidationError):
        locations = [".".join(str(x) for x in row["loc"]) for row in error.errors()]
        return JSONResponse(ErrorEnvelope(code="CONFIG_SCHEMA", message="配置字段不符合契约: " + ", ".join(locations[:8])).model_dump(mode="json"), status_code=422)

    @app.exception_handler(Exception)
    @app.exception_handler(ResponseValidationError)
    async def unexpected_error(_request: Request, _error: Exception):
        return JSONResponse(ErrorEnvelope(code="INTERNAL_ERROR", message="服务处理失败，请查看脱敏诊断", retryable=False).model_dump(mode="json"), status_code=500)

    @app.middleware("http")
    async def request_boundary(request: Request, call_next):
        try:
            supplied_host = request.headers.get("host", "").lower()
            if supplied_host not in allowed_hosts:
                raise HarnessError("HOST_REJECTED", "请求 Host 不在本机服务白名单内", 403)
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                raise HarnessError("ORIGIN_REJECTED", "请求来源未获允许", 403)
            unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
            if unsafe and origin not in allowed_origins:
                raise HarnessError("ORIGIN_REQUIRED", "写请求必须来自已授权的工作台来源", 403)
            if request.url.path.startswith("/api/") and request.url.path not in {
                "/api/auth/exchange",
                "/api/auth/local",
                "/api/mcp/oauth/callback",
                "/api/health",
            }:
                session = auth.authenticate(request.cookies.get("harness_session"))
                request.state.owner_id = session["owner_id"]
                if unsafe:
                    auth.check_csrf(session, request.headers.get("x-csrf-token"))
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
            return response
        except HarnessError as error:
            return JSONResponse(error.envelope().model_dump(mode="json"), status_code=error.status)
        except Exception:
            return JSONResponse(ErrorEnvelope(code="INTERNAL_ERROR", message="服务处理失败，请查看脱敏诊断").model_dump(mode="json"), status_code=500)

    def owner(request: Request) -> str:
        return request.state.owner_id

    async def dispatch(request: Request, action: str, **kwargs):
        return await resolve(services.dispatch(action, owner(request), **kwargs))

    async def mutate(request: Request, action: str, body: dict, **kwargs):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise HarnessError("IDEMPOTENCY_KEY_REQUIRED", "业务写请求需要有效的幂等键", 400)
        callback = lambda: services.dispatch(action, owner(request), **kwargs, body=body)
        return await resolve(services.mutate(owner(request), request.method, request.url.path, key, body, callback))

    def feature(name: str):
        if not capabilities.get(name, False):
            raise HarnessError("FEATURE_NOT_ENABLED", "此功能在当前服务配置中未启用", 501)

    @app.get("/api/agent-modes")
    async def modes(request: Request) -> dict:
        return dict(default="react", policy_version=1, items=[
            dict(id="react", name="ReAct", description="在授权范围内完成任务"),
            dict(id="plan", name="Plan", description="只读分析与计划；模式对本次运行固定")])

    @app.get("/api/mcp-config/file")
    async def mcp_file(request: Request) -> dict:
        return services.mcp_file.view()

    @app.post("/api/mcp-config/validate")
    async def mcp_file_validate(body: McpFileInput, request: Request) -> dict:
        profiles = services.mcp_file.validate(body.text)
        return dict(valid=True, server_ids=list(profiles), connected=False)

    @app.put("/api/mcp-config/file")
    async def mcp_file_save(body: McpFileSaveInput, request: Request) -> dict:
        feature("mcp_file_editing")
        return await mutate(request, "save_mcp_file", body.model_dump())

    @app.post("/api/mcp-config/reload")
    async def mcp_file_reload(request: Request) -> dict:
        return await mutate(request, "reload_mcp_file", {})

    @app.post("/api/mcp/{server_id}/revoke")
    async def mcp_revoke(server_id: str, request: Request) -> dict:
        return await mutate(request, "revoke_mcp", {}, server_id=server_id)

    @app.get("/api/models/available")
    async def available_models(request: Request, session_id: str | None = None) -> dict:
        return services.selection.available(owner(request), session_id)

    @app.get("/api/sessions/{session_id}/preferences")
    async def preferences(session_id: str, request: Request) -> dict:
        return services.selection.preferences(owner(request), session_id)

    @app.patch("/api/sessions/{session_id}/preferences")
    async def save_preferences(session_id: str, body: PreferencesInput, request: Request) -> dict:
        return await mutate(request, "save_preferences", body.model_dump(exclude_none=True), session_id=session_id)

    @app.post("/api/runs/{run_id}/model-selection", status_code=202)
    async def select_model(run_id: str, body: ModelSelectionInput, request: Request) -> dict:
        return await mutate(request, "select_model", body.model_dump(exclude_none=True), run_id=run_id)

    def set_session_cookies(request: Request, response: Response, session: str, csrf: str):
        secure = request.url.scheme == "https"
        response.set_cookie(
            "harness_session",
            session,
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=auth.session_ttl,
        )
        response.set_cookie(
            "harness_csrf",
            csrf,
            httponly=False,
            secure=secure,
            samesite="strict",
            max_age=auth.session_ttl,
        )
        response.headers["X-CSRF-Token"] = csrf

    @app.post("/api/auth/exchange", status_code=204)
    async def exchange(body: PairingInput, request: Request, response: Response):
        session, csrf = auth.exchange(body.ticket, request.client.host if request.client else "")
        set_session_cookies(request, response, session, csrf)

    @app.post("/api/auth/local", status_code=204)
    async def local_exchange(request: Request, response: Response):
        session, csrf = auth.exchange_local(request.client.host if request.client else "")
        set_session_cookies(request, response, session, csrf)

    @app.get("/api/auth/session", response_model=AuthSessionView)
    async def auth_session(request: Request):
        csrf = request.cookies.get("harness_csrf", "")
        auth.check_csrf(auth.authenticate(request.cookies.get("harness_session")), csrf)
        return dict(owner_id=owner(request), csrf_token=csrf, capabilities=capabilities)

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, response: Response):
        auth.logout(request.cookies["harness_session"])
        response.delete_cookie("harness_session")
        response.delete_cookie("harness_csrf")

    @app.get("/api/workspaces", response_model=Page)
    async def workspaces(request: Request, cursor: str | None = None, limit: int = Query(100, ge=1, le=200)):
        return await dispatch(request, "list_workspaces", cursor=cursor, limit=limit)

    @app.get("/api/projects", response_model=Page)
    async def projects(request: Request, cursor: str | None = None, limit: int = Query(100, ge=1, le=200)):
        return await dispatch(request, "list_workspaces", cursor=cursor, limit=limit)

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectInput, request: Request) -> dict:
        return await mutate(request, "create_project", body.model_dump())

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, body: ProjectPatch, request: Request) -> dict:
        return await mutate(request, "update_project", body.model_dump(), project_id=project_id)

    @app.delete("/api/projects/{project_id}")
    async def remove_project(project_id: str, body: ProjectRemoveInput, request: Request) -> dict:
        return await mutate(request, "remove_project", body.model_dump(), project_id=project_id)

    @app.post("/api/local/folder-picker")
    async def folder_picker(request: Request) -> dict:
        from harness.platform.folders import choose_folder
        return await asyncio.to_thread(choose_folder)

    @app.post("/api/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceInput, request: Request) -> dict:
        return await mutate(request, "create_workspace", body.model_dump())

    @app.get("/api/sessions", response_model=Page)
    async def sessions(request: Request, workspace_id: str, archived: bool = False, cursor: str | None = None, limit: int = Query(100, ge=1, le=200)):
        return await dispatch(request, "list_sessions", workspace_id=workspace_id, archived=archived, cursor=cursor, limit=limit)

    @app.post("/api/sessions", status_code=201)
    async def create_session(body: SessionInput, request: Request) -> dict:
        return await mutate(request, "create_session", body.model_dump(exclude_none=True))

    @app.get("/api/sessions/{session_id}")
    async def session(session_id: str, request: Request) -> dict:
        return await dispatch(request, "get_session", session_id=session_id)

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, body: SessionPatch, request: Request) -> dict:
        return await mutate(request, "update_session", body.model_dump(exclude_unset=True), session_id=session_id)

    @app.post("/api/sessions/{session_id}/runs", status_code=202, response_model=SubmitReceipt)
    async def submit_run(session_id: str, body: SubmitRunInput, request: Request):
        return await mutate(request, "submit_run", body.model_dump(mode="json"), session_id=session_id)

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def run(run_id: str, request: Request, view: Annotated[str, Query(pattern="^(state|snapshot)$")] = "state"):
        return await dispatch(request, "get_run", run_id=run_id, view=view)

    @app.get("/api/runs/{run_id}/events", response_class=StreamingResponse)
    async def events(run_id: str, request: Request, after_seq: int | None = Query(None, ge=0), last_event_id: str | None = Header(None)):
        services.store.run(run_id, owner(request))
        cursor = after_seq if after_seq is not None else 0
        if last_event_id:
            prefix, separator, value = last_event_id.rpartition(":")
            if not separator or prefix != run_id or not value.isdigit():
                raise HarnessError("INVALID_EVENT_ID", "事件游标格式或所属任务不匹配", 422)
            header_cursor = int(value)
            if after_seq is not None and after_seq != header_cursor:
                raise HarnessError("CURSOR_CONFLICT", "请求中的两个游标不一致", 400)
            cursor = header_cursor
        first, last = services.store.event_range(run_id)
        if cursor > last:
            raise HarnessError("INVALID_CURSOR", "游标超出当前任务的耐久序列，请重新读取快照", 409)
        if cursor < first - 1:
            raise HarnessError("CURSOR_EXPIRED", "事件游标已过期，请重新读取快照", 409)
        ephemeral_cursor = services.store.ephemeral_cursor(run_id)

        async def stream():
            nonlocal cursor, ephemeral_cursor
            last_heartbeat = time.monotonic()
            while not await request.is_disconnected():
                batch = services.store.events(run_id, cursor, 128)
                size = 0
                for event in batch:
                    data = canonical_json(event)
                    size += len(data.encode())
                    if size > 1024 * 1024:
                        return  # Slow consumer recovers by cursor; never drop a durable event and continue.
                    yield f"id: {run_id}:{event['seq']}\nevent: {event['type']}\ndata: {data}\n\n"
                    cursor = event["seq"]
                for frame in services.store.read_ephemeral(run_id, ephemeral_cursor, 128):
                    event = frame["event"]
                    data = canonical_json(event)
                    if len(data.encode()) <= 262144:
                        yield f"event: message.delta\ndata: {data}\n\n"
                    ephemeral_cursor = frame["id"]
                if batch:
                    continue  # Read through until caught up before waiting, avoiding the subscribe gap.
                if time.monotonic() - last_heartbeat >= 10:
                    try:
                        auth.authenticate(request.cookies.get("harness_session"))
                    except HarnessError:
                        return
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                waiter = getattr(services, "wait_events", None)
                if waiter:
                    await resolve(waiter(run_id, cursor, 0.5))
                else:
                    await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, body: CancelInput, request: Request) -> dict:
        return await mutate(request, "cancel_run", body.model_dump(exclude_none=True), run_id=run_id)

    @app.get("/api/operations/{operation_id}")
    async def operation(operation_id: str, request: Request) -> dict:
        return await dispatch(request, "get_operation", operation_id=operation_id)

    @app.post("/api/operations/{operation_id}/reconcile", status_code=202)
    async def reconcile(operation_id: str, body: ReconciliationInput, request: Request) -> dict:
        return await mutate(request, "reconcile", body.model_dump(mode="json"), operation_id=operation_id)

    @app.get("/api/interactions/{interaction_id}")
    async def interaction(interaction_id: str, request: Request) -> dict:
        return await dispatch(request, "get_interaction", interaction_id=interaction_id)

    @app.post("/api/interactions/{interaction_id}/respond", status_code=202)
    async def respond(interaction_id: str, body: InteractionResponse, request: Request) -> dict:
        return await mutate(request, "respond", body.model_dump(mode="json", exclude_none=True), interaction_id=interaction_id)

    @app.get("/api/runs/{run_id}/context")
    async def context(run_id: str, request: Request) -> dict:
        return await dispatch(request, "get_context", run_id=run_id)

    @app.post("/api/runs/{run_id}/context/pins", status_code=201)
    async def pin_context(run_id: str, body: ContextPinInput, request: Request) -> dict:
        return await mutate(request, "pin_context", body.model_dump(), run_id=run_id)

    @app.post("/api/artifacts", status_code=201, response_model=ArtifactRef)
    async def upload(request: Request, workspace_id: str = Form(...), file: UploadFile = File(...)):
        if (file.content_type or "").startswith("image/"):
            feature("inline_images")
        services.store.workspace(workspace_id, owner(request))
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise HarnessError("IDEMPOTENCY_KEY_REQUIRED", "上传需要有效的幂等键", 400)
        digest, size = hashlib.sha256(), 0
        try:
            while chunk := await file.read(65536):
                size += len(chunk)
                if size > services.artifacts.max_bytes:
                    raise HarnessError("ARTIFACT_TOO_LARGE", "附件超过配置大小上限", 413)
                digest.update(chunk)
            await file.seek(0)
            body = dict(workspace_id=workspace_id, content_hash=digest.hexdigest(), size_bytes=size, mime_type=file.content_type or "application/octet-stream", display_name=Path(file.filename or "attachment").name)

            def write():
                return services.artifacts.put_stream(owner(request), workspace_id, iter(lambda: file.file.read(65536), b""), body["mime_type"], "upload", body["display_name"]).model_dump(mode="json")

            return await resolve(services.mutate(owner(request), request.method, request.url.path, key, body, write))
        finally:
            await file.close()

    @app.get("/api/artifacts/{artifact_id}", response_class=FileResponse)
    async def artifact(artifact_id: str, request: Request, disposition: Annotated[str, Query(pattern="^(attachment|preview)$")] = "attachment"):
        row = services.store.artifact(artifact_id, owner(request))
        path = services.artifacts.path(artifact_id, owner(request))
        filename = Path(row["display_name"]).name.replace("\r", "").replace("\n", "")
        headers = {"Content-Disposition": f"{'inline' if disposition == 'preview' else 'attachment'}; filename*=UTF-8''{quote(filename)}", "Content-Security-Policy": "sandbox; default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'", "Cross-Origin-Resource-Policy": "same-origin"}
        return FileResponse(path, media_type=row["mime_type"], headers=headers)

    @app.get("/api/agents", response_model=Page)
    async def agents(request: Request):
        return await dispatch(request, "list_agents")

    @app.get("/api/config/{kind}", response_model=Page)
    async def configurations(kind: ConfigKind, request: Request):
        return await dispatch(request, "list_config", kind=kind)

    @app.get("/api/config/{kind}/{config_id}")
    async def configuration(kind: ConfigKind, config_id: str, request: Request) -> dict:
        return await dispatch(request, "get_config", kind=kind, config_id=config_id)

    @app.put("/api/config/{kind}/{config_id}")
    async def save_configuration(kind: ConfigKind, config_id: str, body: ConfigInput, request: Request, response: Response) -> dict:
        result = await mutate(request, "save_config", body.model_dump(exclude_unset=True), kind=kind, config_id=config_id)
        response.status_code = 201 if body.expected_revision is None else 200
        return result

    @app.post("/api/models/{model_id}/test")
    async def test_model(model_id: str, body: ModelTestInput, request: Request) -> dict:
        return await mutate(request, "test_model", body.model_dump(), model_id=model_id)

    def model_setup_body(body: ModelDiscoveryInput) -> dict:
        # Raw secrets exist only in this request's memory, never generic config/command payloads.
        value = body.model_dump(exclude_none=True, exclude={"api_key"})
        if body.api_key is not None:
            value["api_key"] = body.api_key.get_secret_value()
        return value

    def check_model_setup_available():
        if (services.store.get("system", "maintenance") or {}).get("enabled"):
            raise HarnessError("MAINTENANCE", "维护期间暂停模型配置，请稍后重试", 503)

    @app.get("/api/model-providers")
    async def model_providers(request: Request) -> dict:
        owner(request)
        return await resolve(services.model_setup.providers())

    @app.post("/api/model-setup/discover")
    async def discover_models(body: ModelDiscoveryInput, request: Request) -> dict:
        check_model_setup_available()
        return await resolve(services.model_setup.discover(owner(request), model_setup_body(body)))

    @app.post("/api/model-setup/test")
    async def test_model_setup(body: ModelSetupInput, request: Request) -> dict:
        check_model_setup_available()
        task = asyncio.create_task(resolve(services.model_setup.test(owner(request), model_setup_body(body))))
        try:
            while not task.done():
                await asyncio.wait({task}, timeout=0.2)
                if await request.is_disconnected():
                    task.cancel()
                    raise HarnessError("MODEL_SETUP_CANCELLED", "检测已中止", 409)
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @app.post("/api/model-setup/save")
    async def save_model_setup(body: ModelSetupSaveInput, request: Request) -> dict:
        check_model_setup_available()
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise HarnessError("IDEMPOTENCY_KEY_REQUIRED", "保存模型需要有效的幂等键", 400)
        payload = model_setup_body(body)
        public_payload = {k: v for k, v in payload.items() if k != "api_key"}
        if "api_key" in payload:
            # Stable within this pairing session, including an API restart. The raw session
            # token and provider key are both absent from the command's durable fingerprint.
            public_payload["api_key_fingerprint"] = hmac.new(
                request.cookies.get("harness_session", "").encode(),
                payload["api_key"].encode(), hashlib.sha256,
            ).hexdigest()
        callback = lambda: services.model_setup.save(owner(request), payload)
        return await resolve(services.mutate(
            owner(request), request.method, request.url.path, key, public_payload, callback
        ))

    @app.get("/api/skills", response_model=Page)
    async def skills(request: Request):
        return await dispatch(request, "list_skills")

    @app.post("/api/skills/refresh")
    async def refresh_skills(body: SkillRefreshInput, request: Request) -> dict:
        return await mutate(request, "refresh_skills", body.model_dump())

    @app.get("/api/skills/{skill_id}")
    async def skill(skill_id: str, request: Request) -> dict:
        return await dispatch(request, "get_skill", skill_id=skill_id)

    @app.patch("/api/skills/{skill_id}")
    async def update_skill(skill_id: str, body: SkillPatch, request: Request) -> dict:
        return await mutate(request, "update_skill", body.model_dump(), skill_id=skill_id)

    @app.get("/api/plugins", response_model=Page)
    async def plugins(request: Request):
        return await dispatch(request, "list_plugins")

    @app.post("/api/plugins", status_code=201)
    async def load_plugin(request: Request, body: PluginLoadInput):
        return await mutate(request, "load_plugin", body.model_dump(mode="json"))

    @app.post("/api/plugins/{plugin_id}/versions/{version}/drain", status_code=202)
    async def drain_plugin(request: Request, plugin_id: str, version: str):
        return await mutate(request, "drain_plugin", {}, plugin_id=plugin_id, version=version)

    @app.get("/api/mcp/oauth/callback")
    async def mcp_callback(request: Request):
        feature("mcp_oauth")
        return await resolve(services.dispatch("mcp_callback", None, params=dict(request.query_params)))

    @app.get("/api/mcp/{server_id}/catalog")
    async def mcp_catalog(server_id: str, request: Request) -> dict:
        return await dispatch(request, "mcp_catalog", server_id=server_id)

    @app.post("/api/mcp/{server_id}/diagnose")
    async def mcp_diagnose(server_id: str, body: McpDiagnoseInput, request: Request) -> dict:
        return await mutate(request, "mcp_diagnose", body.model_dump(), server_id=server_id)

    @app.post("/api/mcp/{server_id}/authorize")
    async def mcp_authorize(server_id: str, request: Request) -> dict:
        feature("mcp_oauth")
        return await mutate(request, "mcp_authorize", {}, server_id=server_id)

    @app.post("/api/sessions/{session_id}/branches", status_code=201)
    async def branch(session_id: str, body: BranchInput, request: Request) -> dict:
        feature("branches")
        return await mutate(request, "create_branch", body.model_dump(mode="json"), session_id=session_id)

    @app.post("/api/runs/{run_id}/input-commands", status_code=202)
    async def steer(run_id: str, body: SteeringInput, request: Request) -> dict:
        feature("steering")
        return await mutate(request, "steer", body.model_dump(), run_id=run_id)

    @app.get("/api/memories", response_model=Page)
    async def memories(request: Request, workspace_id: str, scope: str | None = None, review_state: str | None = None, cursor: str | None = None, limit: int = Query(100, ge=1, le=200)):
        feature("memory")
        return await dispatch(request, "list_memories", workspace_id=workspace_id, scope=scope, review_state=review_state, cursor=cursor, limit=limit)

    @app.get("/api/memories/{memory_id}")
    async def memory(memory_id: str, request: Request) -> dict:
        feature("memory")
        return await dispatch(request, "get_memory", memory_id=memory_id)

    @app.post("/api/memories/{memory_id}/review")
    async def review_memory(memory_id: str, body: MemoryReview, request: Request) -> dict:
        feature("memory")
        return await mutate(request, "review_memory", body.model_dump(), memory_id=memory_id)

    @app.patch("/api/memories/{memory_id}")
    async def update_memory(memory_id: str, body: MemoryPatch, request: Request) -> dict:
        feature("memory")
        return await mutate(request, "update_memory", body.model_dump(exclude_unset=True), memory_id=memory_id)

    @app.delete("/api/memories/{memory_id}", status_code=204)
    async def delete_memory(memory_id: str, request: Request, if_match: str = Header(...)):
        feature("memory")
        try:
            revision = int(if_match.strip('"'))
            if revision < 1:
                raise ValueError()
        except ValueError:
            raise HarnessError("INVALID_REVISION", "If-Match 必须是正整数 revision", 422)
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 200:
            raise HarnessError("IDEMPOTENCY_KEY_REQUIRED", "遗忘请求需要幂等键", 400)
        callback = lambda: services.dispatch("delete_memory", owner(request), memory_id=memory_id, expected_revision=revision)
        await resolve(services.mutate(owner(request), request.method, request.url.path, key, {"expected_revision": revision}, callback))

    @app.get("/health/live")
    async def live() -> dict:
        return {"status": "live", "instance_id": getattr(settings, "instance_id", None)}

    @app.get("/api/diagnostics/metrics")
    async def metrics(request: Request) -> dict:
        owner(request)
        return await resolve(services.metrics.snapshot())

    @app.get("/health/ready")
    @app.get("/api/health")
    async def ready() -> dict:
        callback = getattr(services, "health", None)
        if callback:
            report = await resolve(callback())
            return {key: report[key] for key in ("status", "ready", "api", "worker", "database", "instance_id", "api_alive", "worker_ready", "db_writable", "needs_review_count") if key in report}
        return {"status": "ready" if services.store.integrity()["ok"] else "not_ready"}

    web_root = Path(__file__).resolve().parents[1] / "resources" / "web"
    if not web_root.is_dir():
        web_root = Path(__file__).resolve().parents[2] / "apps" / "web" / "dist"
    if web_root.is_dir():
        app.mount("/", StaticFiles(directory=web_root, html=True), name="web")
    return app
