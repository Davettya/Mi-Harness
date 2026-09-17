"""Standalone local interop fixture; pytest intentionally has no tests here."""
from __future__ import annotations

import asyncio
import os
import sys
from pydantic import BaseModel
from mcp.server.mcpserver import MCPServer, Context
from mcp.types import CallToolResult, TextContent, InputRequiredResult, ElicitRequest, ElicitRequestFormParams


def create_server():
    server = MCPServer("harness-interop-fixture", version="1.0")
    server.fixture_counts = {"ask_initial": 0, "ask_resumed": 0}

    @server.tool()
    def add(a: int, b: int) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=str(a + b))], structured_content={"sum": a + b})

    @server.tool()
    def business_error() -> str:
        raise ValueError("fixture business failure")

    @server.tool()
    def environment_probe() -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text",text="Environment checked")],
            structured_content={"bound_credential":os.environ.get("API_KEY")=="synthetic-vault-fixture",
                                "ambient_secret_absent":"HARNESS_AMBIENT_CANARY" not in os.environ})

    @server.tool()
    async def slow(seconds: float = 10) -> str:
        await asyncio.sleep(seconds)
        return "done"

    class Confirmation(BaseModel):
        confirmed: bool

    @server.tool()
    async def ask(ctx: Context):
        if ctx.protocol_version != "2026-07-28":
            response = await ctx.elicit("Confirm fixture input", Confirmation)
            return response.action
        if ctx.input_responses:
            server.fixture_counts["ask_resumed"] += 1
            response = ctx.input_responses["confirmation"]
            return response.action if hasattr(response, "action") else str(response)
        server.fixture_counts["ask_initial"] += 1
        return InputRequiredResult(input_requests={"confirmation": ElicitRequest(method="elicitation/create",
            params=ElicitRequestFormParams(message="Confirm fixture input", requested_schema=Confirmation.model_json_schema()))},
            request_state="private-continuation-state")

    @server.resource("fixture://greeting")
    def greeting() -> str:
        return "Hello from a sourced MCP resource"

    @server.prompt()
    def explain(topic: str) -> str:
        return "Explain " + topic

    return server


if __name__ == "__main__":
    fixture = create_server()
    fixture.run(transport="stdio")
