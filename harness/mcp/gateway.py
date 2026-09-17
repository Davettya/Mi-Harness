from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Callable

import jsonschema
from mcp.shared.exceptions import MCPError
from mcp.types import InputRequiredResult
from harness.core.contracts import DiagnosticContext, ExecutionContext, content_hash
from .client_factory import resolve
from .models import (CatalogSnapshot, CatalogTool, McpInvocation, McpOutcome, CancelOutcome,
                     DiagnosticReport, SourcedContent, McpInputRequired)


def _dump(value):
    return value.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value


def _field(value, snake, camel=None, default=None):
    if isinstance(value, dict):
        return value.get(camel or snake, value.get(snake, default))
    return getattr(value, snake, getattr(value, camel or snake, default))


class _RunConnection:
    """An owner task enters/exits anyio contexts; callers never close its scopes."""
    def __init__(self, factory, profile, context, diagnostics):
        self.factory, self.profile, self.context, self.diagnostics = factory, profile, context, diagnostics
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._serve())

    async def _serve(self):
        try:
            async with self.factory.connect(self.profile, self.context, "run", self.diagnostics) as client:
                self.ready.set_result(client)
                while True:
                    item = await self.queue.get()
                    if item is None:
                        break
                    callback, result = item
                    if result.cancelled():
                        continue
                    operation = asyncio.create_task(callback(client))
                    def cancelled(future, target=operation):
                        if future.cancelled():
                            target.cancel()
                    result.add_done_callback(cancelled)
                    try:
                        value = await operation
                        if not result.done():
                            result.set_result(value)
                    except BaseException as exc:
                        if not result.done():
                            if isinstance(exc, asyncio.CancelledError):
                                result.cancel()
                            else:
                                result.set_exception(exc)
                    finally:
                        result.remove_done_callback(cancelled)
        except BaseException as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item and not item[1].done():
                    item[1].set_exception(RuntimeError("MCP scope connection closed"))

    async def execute(self, callback):
        await self.ready
        if self.task.done():
            raise RuntimeError("MCP run scope closed")
        result = asyncio.get_running_loop().create_future()
        await self.queue.put((callback, result))
        return await result

    async def close(self):
        await self.queue.put(None)
        await self.task


class McpGateway:
    def __init__(self, profile_resolver, client_factory, *, invocation_validator: Callable,
                 max_pages=100, max_tools=2000, principal_fingerprint=None, profile_snapshot_resolver=None):
        self.profile_resolver, self.factory = profile_resolver, client_factory
        self.invocation_validator = invocation_validator
        self.max_pages, self.max_tools = max_pages, max_tools
        self.principal_fingerprint = principal_fingerprint
        self.profile_snapshot_resolver = profile_snapshot_resolver
        self._cache, self._scopes, self._inflight = {}, {}, {}

    async def _profile(self, ref, context=None):
        if self.profile_snapshot_resolver is not None and isinstance(context, ExecutionContext):
            return await resolve(self.profile_snapshot_resolver(ref, context))
        return await resolve(self.profile_resolver(ref))

    async def _identity(self, profile, context):
        credential_identity = profile.credential_ref or profile.oauth_ref or "anonymous"
        if self.principal_fingerprint:
            credential_identity = await resolve(self.principal_fingerprint(profile, context))
        elif profile.credential_ref and getattr(self.factory, "credential_resolver", None):
            value = await resolve(self.factory.credential_resolver(profile.credential_ref, profile.server_id, context))
            credential_identity = hashlib.sha256(value.encode()).hexdigest()
        return content_hash({"owner": context.owner_id, "workspace": context.workspace_id,
            "server": profile.server_id, "credentials": credential_identity,
            "scope": getattr(context, "run_id", getattr(context, "diagnostic_id", None)),
            "revision": profile.config_revision, "protocol": profile.protocol_policy})

    async def _execute(self, profile, context, purpose, diagnostics, callback):
        await resolve(self.factory.authorize(profile, context, purpose))
        timeout = profile.max_total_duration
        deadline = getattr(context, "deadline_at", None)
        if deadline:
            remaining = (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds()
            timeout = min(timeout, remaining)
        if timeout <= 0:
            raise TimeoutError("access deadline expired")
        if profile.connection_scope == "run" and isinstance(context, ExecutionContext):
            key = await self._identity(profile, context)
            if key not in self._scopes:
                self._scopes[key] = _RunConnection(self.factory, profile, context, diagnostics)
            async with asyncio.timeout(timeout):
                return await self._scopes[key].execute(callback)
        async with asyncio.timeout(timeout):
            async with self.factory.connect(profile, context, purpose, diagnostics) as client:
                return await callback(client)

    async def inspect(self, profile_ref, access_context):
        profile = await self._profile(profile_ref, access_context)
        diagnostics = []
        async def inspect_client(client):
            return {"server_id": profile.server_id, "transport": profile.transport,
                    "protocol_version": client.protocol_version, "capabilities": _dump(client.server_capabilities),
                    "server_info": _dump(client.server_info), "diagnostics": diagnostics,
                    "tool_execution": "not_executed"}
        return await self._execute(profile, access_context, "inspect", diagnostics, inspect_client)

    async def discover(self, profile_ref, access_context, cache_policy="use"):
        profile = await self._profile(profile_ref, access_context)
        await resolve(self.factory.authorize(profile, access_context, "discover"))
        identity = await self._identity(profile, access_context)
        cached = self._cache.get(identity)
        if cache_policy == "use" and cached and cached[0] > time.monotonic():
            return cached[1]
        diagnostics = []
        async def list_all(client):
            tools, seen, aliases, cursor = [], set(), set(), None
            ttl = profile.catalog_ttl
            complete = False
            for _ in range(self.max_pages):
                page = await client.list_tools(cursor=cursor)
                hint = _field(page, "ttl_ms", "ttlMs")
                if hint is not None:
                    ttl = min(ttl, max(0, hint / 1000))
                for tool in _field(page, "tools", default=[]):
                    original = _field(tool, "name")
                    input_schema = _field(tool, "input_schema", "inputSchema", {})
                    output_schema = _field(tool, "output_schema", "outputSchema")
                    try:
                        jsonschema.Draft202012Validator.check_schema(input_schema)
                        if output_schema is not None:
                            jsonschema.Draft202012Validator.check_schema(output_schema)
                    except jsonschema.SchemaError:
                        diagnostics.append(f"invalid_schema:{original}")
                        continue
                    digest = content_hash({"input": input_schema, "output": output_schema})
                    revision = str(profile.config_revision) + ":" + digest[:16]
                    suffix = hashlib.sha256((profile.server_id + "/" + original + "/" + revision).encode()).hexdigest()[:14]
                    alias = "mcp_" + re.sub(r"[^a-zA-Z0-9_-]", "_", original)[:40] + "_" + suffix
                    if alias in aliases:
                        diagnostics.append("duplicate_tool_alias")
                        return tools, False, ttl
                    aliases.add(alias)
                    tools.append(CatalogTool(original_name=original, safe_alias=alias, schema_hash=digest,
                        input_schema=input_schema, output_schema=output_schema,
                        annotations=_dump(_field(tool, "annotations")) or {},
                        description=_field(tool, "description", default="") or "", revision=revision))
                    if len(tools) >= self.max_tools:
                        diagnostics.append("catalog_tool_limit")
                        return tools, False, ttl
                next_cursor = _field(page, "next_cursor", "nextCursor")
                if next_cursor is None:
                    complete = True
                    break
                if next_cursor in seen:
                    diagnostics.append("repeated_cursor")
                    break
                seen.add(next_cursor)
                cursor = next_cursor  # Empty string is a valid opaque continuation.
            else:
                diagnostics.append("catalog_page_limit")
            return tools, complete, ttl

        async def collect(client):
            tools, complete, ttl = await list_all(client)
            snapshot = CatalogSnapshot(server_id=profile.server_id, principal_fingerprint=identity,
                config_revision=profile.config_revision, protocol_version=client.protocol_version,
                expires_at=(datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat(),
                complete=complete, diagnostics=diagnostics, tools=tools)
            return snapshot, ttl
        snapshot, ttl = await self._execute(profile, access_context, "discover", diagnostics, collect)
        self._cache[identity] = (time.monotonic() + ttl, snapshot)
        return snapshot

    async def diagnose(self, profile_ref, level, diagnostic_context):
        if not isinstance(diagnostic_context, DiagnosticContext):
            raise TypeError("settings diagnostics require DiagnosticContext")
        profile = await self._profile(profile_ref)
        layers = {name: {"status": "not_executed"} for name in
                  ("transport", "authentication", "protocol", "discovery", "schema", "example_tool")}
        observed, diagnostics = {}, []
        try:
            observed = await self.inspect(profile_ref, diagnostic_context)
            for name in ("transport", "protocol"):
                layers[name] = {"status": "passed"}
            layers["authentication"] = {"status": "passed" if profile.credential_ref or profile.oauth_ref else "not_required"}
            if level in ("catalog", "full", "schema"):
                catalog = await self.discover(profile_ref, diagnostic_context, "refresh")
                diagnostics.extend(catalog.diagnostics)
                layers["discovery"] = {"status": "passed" if catalog.complete else "failed", "count": len(catalog.tools)}
                layers["schema"] = {"status": "failed" if any(d.startswith("invalid_schema:") for d in diagnostics) else "passed"}
        except Exception as exc:
            # Exception bodies may include credentials or remote content. Only class is public.
            phase = "transport" if not observed else "discovery"
            layers[phase] = {"status": "failed", "error_code": type(exc).__name__}
            diagnostics.append(type(exc).__name__)
        return DiagnosticReport(server_id=profile.server_id, diagnostic_id=diagnostic_context.diagnostic_id,
            protocol_version=observed.get("protocol_version"), capabilities=observed.get("capabilities", {}),
            layers=layers, diagnostics=diagnostics)

    async def invoke(self, invocation: McpInvocation):
        return await self._invoke(invocation)

    async def invoke_step(self, invocation: McpInvocation, *, input_responses=None, request_state=None):
        """Single MRTR exchange for the durable Host interaction controller."""
        return await self._invoke(invocation, continuation_mode=True, input_responses=input_responses, request_state=request_state)

    async def _invoke(self, invocation: McpInvocation, *, continuation_mode=False, input_responses=None, request_state=None):
        if not isinstance(invocation.execution_context, ExecutionContext) or not invocation.policy_decision_ref:
            raise PermissionError("MCP invocation requires Tool Gateway authorization")
        if invocation.operation_key.run_id != invocation.execution_context.run_id:
            raise PermissionError("operation belongs to another run")
        # Host validates the ledger/policy reference, not merely a caller-controlled string.
        await resolve(self.invocation_validator(invocation))
        profile = await self._profile(invocation.server_id, invocation.execution_context)
        if "tools" not in profile.allowed_capabilities:
            raise PermissionError("MCP tools capability disabled")
        diagnostics, sent = [], False
        try:
            catalog = await self.discover(invocation.server_id, invocation.execution_context, "refresh")
            tool = next((t for t in catalog.tools if t.original_name == invocation.original_name), None)
            if not tool or tool.schema_hash != invocation.schema_hash:
                raise ValueError("MCP tool schema changed; explicit revalidation required")
            jsonschema.validate(invocation.arguments, tool.input_schema)
            deadline = invocation.deadline or invocation.execution_context.deadline_at
            timeout = min(profile.request_timeout, profile.max_total_duration)
            if deadline:
                timeout = min(timeout, (datetime.fromisoformat(deadline.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
            if timeout <= 0:
                raise TimeoutError("execution deadline expired")

            async def call(client):
                nonlocal sent
                if continuation_mode and client.protocol_version != "2026-07-28":
                    raise ValueError("durable elicitation requires modern MRTR; legacy backchannel cannot survive restart")
                sent = True  # Conservatively mark before SDK dispatch; no safe replay inference.
                binding = getattr(client, "_harness_binding", None)
                if binding is not None:
                    binding.update(invocation=invocation, diagnostics=diagnostics, interaction_rounds=0)
                try:
                    if continuation_mode:
                        result = await client.session.call_tool(invocation.original_name, invocation.arguments,
                            input_responses=input_responses, request_state=request_state, allow_input_required=True)
                        if isinstance(result, InputRequiredResult):
                            return McpInputRequired(server_id=profile.server_id, operation_id=invocation.operation_id,
                                protocol_version=client.protocol_version, input_requests={key: _dump(value) for key, value in (result.input_requests or {}).items()},
                                request_state=result.request_state)
                        return result
                    return await client.call_tool(invocation.original_name, invocation.arguments)
                finally:
                    if binding is not None:
                        binding["invocation"] = None

            operation = asyncio.create_task(self._execute(profile, invocation.execution_context, "invoke", diagnostics, call))
            self._inflight[invocation.operation_id] = operation
            async with asyncio.timeout(timeout):
                result = await operation
            if isinstance(result, McpInputRequired):
                return result
            error = bool(_field(result, "is_error", "isError", False))
            return McpOutcome(kind="tool_error" if error else "success", server_id=profile.server_id,
                operation_id=invocation.operation_id, request_sent=sent,
                content=[_dump(block) for block in _field(result, "content", default=[])],
                structured_content=_field(result, "structured_content", "structuredContent"),
                is_error=error, diagnostics=diagnostics, remote_operation_ref=invocation.remote_operation_ref)
        except asyncio.CancelledError:
            return McpOutcome(kind="cancelled", server_id=profile.server_id, operation_id=invocation.operation_id,
                request_sent=sent, cancellation_evidence="local_request_cancelled; remote side effects not rolled back", diagnostics=diagnostics)
        except TimeoutError:
            return McpOutcome(kind="unknown_outcome" if sent else "timeout", server_id=profile.server_id,
                operation_id=invocation.operation_id, request_sent=sent, error_code="deadline_exceeded", diagnostics=diagnostics)
        except MCPError as exc:
            return McpOutcome(kind="protocol_error", server_id=profile.server_id, operation_id=invocation.operation_id,
                request_sent=sent, error_code=str(exc.code), diagnostics=diagnostics)
        except (ValueError, jsonschema.ValidationError) as exc:
            return McpOutcome(kind="protocol_error", server_id=profile.server_id, operation_id=invocation.operation_id,
                request_sent=sent, error_code=type(exc).__name__, diagnostics=diagnostics)
        except Exception as exc:
            # AnyIO context exits can wrap an ordinary protocol error in task groups.
            # Inspect classes only; never log exception messages or response bodies.
            leaves = []
            def collect(error):
                if isinstance(error, BaseExceptionGroup):
                    for child in error.exceptions:
                        collect(child)
                else:
                    leaves.append(error)
            collect(exc)
            if leaves and all(isinstance(error, MCPError) for error in leaves):
                return McpOutcome(kind="protocol_error", server_id=profile.server_id, operation_id=invocation.operation_id,
                    request_sent=sent, error_code=str(leaves[0].code), diagnostics=diagnostics)
            return McpOutcome(kind="unknown_outcome" if sent else "transport_error", server_id=profile.server_id,
                operation_id=invocation.operation_id, request_sent=sent, error_code=type(exc).__name__, diagnostics=diagnostics)
        finally:
            self._inflight.pop(invocation.operation_id, None)

    async def cancel(self, operation_id):
        operation = self._inflight.get(operation_id)
        if not operation:
            return CancelOutcome(accepted=False, error="operation_not_inflight")
        operation.cancel()
        return CancelOutcome(accepted=True, transport_closed=False, remote_confirmed=None)

    async def close_scope(self, scope_ref):
        closed = 0
        for key, scope in list(self._scopes.items()):
            if getattr(scope.context, "run_id", None) == scope_ref:
                await scope.close()
                del self._scopes[key]
                closed += 1
        return {"closed": closed}

    async def aclose(self):
        for task in list(self._inflight.values()):
            task.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight.values(), return_exceptions=True)
        for key, scope in list(self._scopes.items()):
            await scope.close()
            self._scopes.pop(key, None)

    async def read_resource(self, server_ref, uri, execution_context):
        return await self._content(server_ref, execution_context, "resources", uri, {})

    async def get_prompt(self, server_ref, name, arguments, execution_context):
        return await self._content(server_ref, execution_context, "prompts", name, arguments)

    async def _content(self, server_ref, context, capability, reference, arguments):
        profile = await self._profile(server_ref, context)
        if capability not in profile.allowed_capabilities:
            raise PermissionError(f"MCP {capability} capability disabled")
        diagnostics = []
        async def fetch(client):
            result = await client.read_resource(reference) if capability == "resources" else await client.get_prompt(reference, arguments)
            return SourcedContent(server_id=profile.server_id, reference=reference,
                protocol_version=client.protocol_version, content=_dump(result))
        return await self._execute(profile, context, capability, diagnostics, fetch)

    async def list_content(self, server_ref, capability, context):
        if capability not in ("resources", "prompts"):
            raise ValueError("only resources and prompts have content catalogs")
        profile = await self._profile(server_ref, context)
        if capability not in profile.allowed_capabilities:
            raise PermissionError("content capability disabled")
        async def collect(client):
            cursor, seen, results = None, set(), []
            for _ in range(self.max_pages):
                page = await getattr(client, "list_" + capability)(cursor=cursor)
                results.extend(_dump(item) for item in _field(page, capability, default=[]))
                if len(results) > self.max_tools:
                    raise ValueError("content catalog limit exceeded")
                cursor = _field(page, "next_cursor", "nextCursor")
                if cursor is None:
                    return results
                if cursor in seen:
                    raise ValueError("repeated content cursor")
                seen.add(cursor)
            raise ValueError("content page limit exceeded")
        return await self._execute(profile, context, "discover", [], collect)
