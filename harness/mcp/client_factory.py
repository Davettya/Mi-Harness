"""The only SDK integration seam; verified against mcp 2.2.0 public signatures."""
from __future__ import annotations

import asyncio
import inspect
import os
import threading
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp.types import ElicitResult


async def resolve(value):
    return await value if inspect.isawaitable(value) else value


class _BoundedStderr:
    def __init__(self, limit=65536):
        read_fd, write_fd = os.pipe()
        self.writer = os.fdopen(write_fd, "w", encoding="utf-8")
        self.data, self.truncated, self.limit = bytearray(), False, limit
        def drain():
            with os.fdopen(read_fd, "rb") as stream:
                while block := stream.read(4096):
                    remaining = max(0, self.limit - len(self.data))
                    self.data.extend(block[:remaining])
                    self.truncated |= len(block) > remaining
        self.thread = threading.Thread(target=drain, daemon=True, name="mcp-stderr")
        self.thread.start()

    def close(self):
        self.writer.close()
        self.thread.join(timeout=2)


class McpClientFactory:
    def __init__(self, *, authorize, credential_resolver=None, cwd_resolver=None,
                 oauth_resolver=None, elicitation_bridge=None, outbound_authorize=None):
        self.authorize = authorize
        self.credential_resolver = credential_resolver
        self.cwd_resolver = cwd_resolver
        self.oauth_resolver = oauth_resolver
        self.elicitation_bridge = elicitation_bridge
        self.outbound_authorize = outbound_authorize

    @asynccontextmanager
    async def connect(self, profile, context, purpose, diagnostics):
        await resolve(self.authorize(profile, context, purpose))
        binding = {"diagnostics": diagnostics, "invocation": None, "interaction_rounds": 0}

        async def elicitation(request_context, params):
            binding["interaction_rounds"] += 1
            interaction_rounds = binding["interaction_rounds"]
            if ("elicitation" not in profile.allowed_capabilities or self.elicitation_bridge is None
                    or not getattr(context, "run_id", None) or not binding["invocation"] or interaction_rounds > 8):
                binding["diagnostics"].append("unsupported_elicitation" if interaction_rounds <= 8 else "elicitation_round_limit")
                return ElicitResult(action="cancel")
            result = await resolve(self.elicitation_bridge(profile, context, params, binding["invocation"]))
            return result if isinstance(result, ElicitResult) else ElicitResult.model_validate(result)

        async with AsyncExitStack() as stack:
            mode = "legacy" if profile.protocol_policy == "legacy_only" else "auto"
            if profile.transport == "stdio":
                env = {}
                for name, ref in profile.environment_refs.items():
                    if not self.credential_resolver:
                        raise PermissionError("credential resolver unavailable")
                    env[name] = await resolve(self.credential_resolver(ref, profile.server_id, context))
                cwd = None
                if profile.cwd_ref:
                    if not self.cwd_resolver:
                        raise PermissionError("workspace resolver unavailable")
                    cwd = await resolve(self.cwd_resolver(profile.cwd_ref, context))
                params = StdioServerParameters(command=profile.command, args=profile.args, env=env, cwd=cwd)
                stderr = _BoundedStderr()
                stack.callback(stderr.close)
                target = stdio_client(params, errlog=stderr.writer)
            else:
                headers, auth = {}, None
                async def outbound(request):
                    if self.outbound_authorize:
                        await resolve(self.outbound_authorize(str(request.url), context, "mcp"))
                if profile.credential_ref:
                    if not self.credential_resolver:
                        raise PermissionError("credential resolver unavailable")
                    secret = await resolve(self.credential_resolver(profile.credential_ref, profile.server_id, context))
                    headers["Authorization"] = "Bearer " + secret
                if profile.oauth_ref:
                    if not self.oauth_resolver:
                        raise PermissionError("OAuth provider unavailable")
                    if not self.outbound_authorize:
                        raise PermissionError("OAuth requires per-request outbound authorization")
                    auth = await resolve(self.oauth_resolver(profile.oauth_ref, profile, context))
                if profile.transport == "legacy_sse":
                    def controlled_http_client(headers=None, timeout=None, auth=None):
                        return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth,
                            trust_env=False, follow_redirects=False, event_hooks={"request": [outbound]})
                    target = sse_client(profile.endpoint, headers=headers, auth=auth,
                        timeout=profile.connect_timeout, sse_read_timeout=profile.request_timeout,
                        httpx_client_factory=controlled_http_client)
                else:
                    http = await stack.enter_async_context(httpx2.AsyncClient(headers=headers, auth=auth,
                        timeout=httpx2.Timeout(profile.request_timeout, connect=profile.connect_timeout),
                        follow_redirects=False, trust_env=False, event_hooks={"request": [outbound]}))
                    target = streamable_http_client(profile.endpoint, http_client=http)
            client = Client(target, mode=mode, read_timeout_seconds=profile.request_timeout,
                elicitation_callback=elicitation, input_required_max_rounds=8, cache=None)
            client._harness_binding = binding
            # SDK owns negotiation, modern metadata, MRTR and legacy notifications.
            # Enter timeout does not outlive its cancel scope: it encloses the stack exit.
            async with asyncio.timeout(profile.connect_timeout):
                await stack.enter_async_context(client)
            if profile.protocol_policy == "modern_only" and client.protocol_version != "2026-07-28":
                raise ValueError("modern_only policy rejected negotiated legacy protocol")
            yield client
