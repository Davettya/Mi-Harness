"""Validate full LangChain messages; never expose partial tool arguments."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import jsonschema
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message

from .profiles import GatewayError, ModelProfile, Usage


def validate_history(messages: list[BaseMessage], *, allow_pending: bool = False) -> set[str]:
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise GatewayError("invalid_history", "Tool result has no matching pending call")
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise GatewayError("invalid_history", "Assistant tool calls and results must remain paired")
            if isinstance(message, AIMessage):
                for call in message.tool_calls:
                    if not call.get("id") or call["id"] in seen:
                        raise GatewayError("invalid_history", "Missing or repeated tool-call ID")
                    seen.add(call["id"])
                    pending.add(call["id"])
    if pending and not allow_pending:
        raise GatewayError("invalid_history", "Cannot call a model with unresolved tool calls")
    return pending


@dataclass(frozen=True)
class ValidatedAssistantTurn:
    message: AIMessage
    usage: Usage
    provider_metadata: dict[str, Any]


def validate_response(message: AIMessage, schemas: dict[str, dict] | None = None) -> ValidatedAssistantTurn:
    if not isinstance(message, AIMessage) or isinstance(message, AIMessageChunk):
        raise GatewayError("invalid_response", "A complete assistant message is required")
    if message.invalid_tool_calls:
        raise GatewayError("invalid_tool_arguments", "Provider returned malformed tool arguments")
    seen: set[str] = set()
    for call in message.tool_calls:
        if not call.get("id") or call["id"] in seen:
            raise GatewayError("invalid_tool_id", "Tool-call IDs must be present and unique")
        seen.add(call["id"])
        if not isinstance(call.get("args"), dict):
            raise GatewayError("invalid_tool_arguments", "Tool arguments must be an object")
        if schemas is not None:
            if call["name"] not in schemas:
                raise GatewayError("unknown_tool", "Provider requested a tool outside the bound catalog")
            try:
                jsonschema.Draft202012Validator(schemas[call["name"]]).validate(call["args"])
            except jsonschema.ValidationError as exc:
                raise GatewayError(
                    "invalid_tool_arguments", "Tool arguments violate the bound schema"
                ) from exc
    finish = message.response_metadata.get("finish_reason") or message.response_metadata.get("stop_reason")
    if finish in {"length", "max_tokens", "content_filter"}:
        raise GatewayError("truncated_response", "Model response did not complete normally")
    if not message.id:
        message = message.model_copy(update={"id": str(uuid4())})
    metadata = message.usage_metadata
    usage = (
        Usage(
            **{key: metadata.get(key) for key in ("input_tokens", "output_tokens", "total_tokens")},
            source="provider",
        )
        if metadata
        else Usage()
    )
    return ValidatedAssistantTurn(message, usage, message.response_metadata)


class StreamAssembler:
    def __init__(self, attempt_id: str):
        self.attempt_id = attempt_id
        self.chunk: AIMessageChunk | None = None
        self.finished = False

    def push(self, attempt_id: str, chunk: AIMessageChunk) -> None:
        if attempt_id != self.attempt_id or self.finished:
            raise GatewayError("stream_state", "Attempt stream identity or completion mismatch")
        if isinstance(chunk, AIMessage) and not isinstance(chunk, AIMessageChunk):
            chunk = AIMessageChunk(**chunk.model_dump(exclude={"type"}))
        self.chunk = chunk if self.chunk is None else self.chunk + chunk

    def finish(self, *, completed: bool, schemas: dict[str, dict] | None = None) -> ValidatedAssistantTurn:
        self.finished = True
        if not completed or self.chunk is None:
            raise GatewayError("incomplete_stream", "Stream ended before complete response")
        # LangChain's partial JSON parser intentionally repairs truncated strings for UI use.
        # Execution requires strict JSON parsing of the original accumulated argument bytes.
        for call in self.chunk.tool_call_chunks:
            try:
                args = json.loads(call.get("args") or "")
            except (ValueError, TypeError) as exc:
                raise GatewayError(
                    "invalid_tool_arguments", "Stream tool arguments are not complete JSON"
                ) from exc
            if not isinstance(args, dict):
                raise GatewayError("invalid_tool_arguments", "Tool arguments must be an object")
        return validate_response(message_chunk_to_message(self.chunk), schemas)


def check_model_switch(messages: list[BaseMessage], previous: ModelProfile, target: ModelProfile) -> None:
    validate_history(messages)
    if previous.ref != target.ref and previous.continuation_requirements.get("portable") is False:
        raise GatewayError(
            "nonportable_history", "Provider continuation state requires rebuilding standard history"
        )
