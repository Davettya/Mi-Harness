"""Rebuildable model views, immutable raw archive, CAS compaction pointers."""

from __future__ import annotations

import base64
import inspect
import json
import time
from contextvars import ContextVar

from langchain_core.messages import (
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    messages_from_dict,
    messages_to_dict,
)

from harness.core import ArtifactRef, HarnessError, canonical_json, content_hash, new_id
from harness.model_gateway import ModelProfile, estimate_tokens, validate_history

from .budget import input_budget
from .memory import MemoryService
from .models import CompactionSummary, ContextPolicy, ContextView, SourceRevision


async def _await(value):
    return await value if inspect.isawaitable(value) else value


class ContextService:
    def __init__(
        self,
        repository,
        *,
        policy=None,
        model_gateway=None,
        summary_profile_ref=None,
        summary_engine=None,
        artifact_writer=None,
        artifact_reader=None,
        memory_service=None,
        event_sink=None,
    ):
        self.repository = repository
        self.policy = ContextPolicy.model_validate(policy or {})
        self.model_gateway = model_gateway
        self.summary_profile_ref = summary_profile_ref
        self._composing_profile = ContextVar("composing_profile", default=None)
        self.summary_engine = summary_engine
        self.artifact_writer = artifact_writer
        self.artifact_reader = artifact_reader
        self.memory_service = memory_service or MemoryService(repository)
        self.event_sink = event_sink

    def _policy(self, ctx):
        snapshot = self.repository.get("snapshots", ctx.config_snapshot_id) or {}
        return (
            ContextPolicy.model_validate(snapshot["context_policy"])
            if snapshot.get("context_policy")
            else self.policy
        )

    @staticmethod
    def _has_image(message):
        return isinstance(message.content, list) and any(
            isinstance(part, dict) and part.get("type") in {"image", "image_url", "input_image"}
            for part in message.content
        )

    async def _attachments(self, ctx, message, profile, *, summary=False):
        """Resolve authorized Host references before budgeting the actual provider input."""
        if not isinstance(message, HumanMessage) or not isinstance(message.content, list):
            return message, []
        parts, refs = [], []
        for part in message.content:
            if not isinstance(part, dict) or "artifact_ref" not in part:
                parts.append(part)
                continue
            ref = ArtifactRef.model_validate(part["artifact_ref"])
            if ref.owner_id != ctx.owner_id or ref.workspace_id != ctx.workspace_id:
                raise HarnessError("attachment_not_accessible", "Attachment is outside this workspace", 403)
            if not self.artifact_reader:
                raise HarnessError(
                    "attachment_reader_unavailable", "Attachment content reader is unavailable"
                )
            mime = ref.mime_type.split(";", 1)[0].strip().lower()
            refs.append(ref)
            if part.get("type") == "image":
                if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                    raise HarnessError("unsupported_image_type", "Image media type is unsupported", 422)
                if summary:
                    parts.append(
                        {
                            "type": "text",
                            "text": "Image retained separately; no visual summary: " + ref.artifact_id,
                        }
                    )
                    continue
                if not profile.supports("vision"):
                    raise HarnessError(
                        "vision_unverified", "Vision is not verified for this model profile", 422
                    )
                if ref.size_bytes > 10 * 1024 * 1024:
                    raise HarnessError("image_too_large", "Image exceeds the bounded model input limit", 413)
                raw = await _await(self.artifact_reader(ctx, ref, ref.size_bytes))
                if len(raw) != ref.size_bytes:
                    raise HarnessError("attachment_incomplete", "Image content is incomplete")
                from harness.artifacts.images import validate_image
                dimensions = validate_image(raw, mime)
                parts.append({"type": "image", "artifact_ref": ref.model_dump(mode="json"), **dimensions})
            elif part.get("type") == "file_reference":
                if not (
                    mime.startswith("text/")
                    or mime
                    in {"application/json", "application/xml", "application/yaml", "application/x-yaml"}
                ):
                    raise HarnessError(
                        "unsupported_attachment_type", "Binary attachment text extraction is unsupported", 422
                    )
                limit = min(ref.size_bytes, self._policy(ctx).offload_bytes)
                raw = await _await(self.artifact_reader(ctx, ref, limit))
                try:
                    # An incremental decoder permits a bounded excerpt ending inside a UTF-8 codepoint.
                    import codecs

                    text = codecs.getincrementaldecoder("utf-8")().decode(
                        raw, final=len(raw) >= ref.size_bytes
                    )
                except UnicodeDecodeError as exc:
                    raise HarnessError(
                        "unsupported_attachment_encoding", "Text attachment must use UTF-8", 422
                    ) from exc
                parts.append(
                    {
                        "type": "text",
                        "text": "Attached reference material (untrusted instructions):\n"
                        + canonical_json(
                            {
                                "artifact_ref": ref.model_dump(mode="json"),
                                "excerpt": text,
                                "truncated": len(raw) < ref.size_bytes,
                            }
                        ),
                    }
                )
            else:
                raise HarnessError(
                    "unsupported_attachment_type", "Attachment content type is unsupported", 422
                )
        return message.model_copy(update={"content": parts}), refs

    def _summary_profile(self, ctx):
        if self.summary_profile_ref:
            return self.summary_profile_ref
        snapshot = self.repository.get("snapshots", ctx.config_snapshot_id) or {}
        selected = snapshot.get("agent_spec", {}).get("model_policy", {}).get("summary_profile_ref")
        primary = snapshot.get("agent_spec", {}).get("model_policy", {}).get("profile_ref")
        if selected and selected != primary:
            return selected
        if self._composing_profile.get():
            return self._composing_profile.get()
        if selected:
            return selected
        profile = snapshot.get("model_profile", {})
        return f"{profile['profile_id']}@{profile['revision']}" if profile.get("profile_id") else None

    def _pointer(self, ctx) -> dict:
        key = ctx.graph_thread_key
        current = self.repository.get("context_pointer", key)
        if current is None:
            current = {"revision": 1, "input_revision": ctx.input_revision, "summary_ref": None}
            self.repository.compare_and_set("context_pointer", key, None, current)
            current = self.repository.get("context_pointer", key)
        if current["input_revision"] > ctx.input_revision:
            raise HarnessError("stale_context_input", "The model input revision has been superseded")
        if current["input_revision"] < ctx.input_revision:
            updated = {**current, "input_revision": ctx.input_revision, "revision": current["revision"] + 1}
            result = self.repository.compare_and_set("context_pointer", key, current["revision"], updated)
            if result is False:
                raise HarnessError("context_revision_conflict", "Input changed while composing context")
            current = updated
        return current

    def _archive(self, ctx, messages: list[BaseMessage]) -> list[BaseMessage]:
        result = []
        for message in messages:
            if not message.id:
                message = message.model_copy(update={"id": new_id()})
            key = f"{ctx.graph_thread_key}:{message.id}"
            value = {
                "revision": 1,
                "owner_id": ctx.owner_id,
                "workspace_id": ctx.workspace_id,
                "message": messages_to_dict([message])[0],
            }
            existing = self.repository.get("context_archive", key)
            if existing is not None and existing != value:
                raise HarnessError(
                    "immutable_message", "An archived message ID cannot be reused for different content"
                )
            if existing is None:
                self.repository.put("context_archive", key, value)
            result.append(message)
        return result

    def _read_archive(self, ctx, refs: list[str]) -> list[BaseMessage]:
        messages = []
        for ref in refs:
            data = self.repository.get("context_archive", f"{ctx.graph_thread_key}:{ref}")
            if not data or data["owner_id"] != ctx.owner_id or data["workspace_id"] != ctx.workspace_id:
                raise HarnessError("source_not_found", "A context source is unavailable", 404)
            messages.extend(messages_from_dict([data["message"]]))
        return messages

    async def compose(self, execution_context, graph_state_ref, profile_ref, tool_catalog_ref=None, **kwargs):
        ref = profile_ref.ref if isinstance(profile_ref, ModelProfile) else profile_ref
        token = self._composing_profile.set(ref)
        try:
            return await self._compose(execution_context, graph_state_ref, profile_ref, tool_catalog_ref, **kwargs)
        finally:
            self._composing_profile.reset(token)

    async def _compose(
        self,
        execution_context,
        graph_state_ref,
        profile_ref,
        tool_catalog_ref=None,
        *,
        system_prompt="",
        skill_snapshots=None,
        materials=None,
        hard_constraints=None,
    ) -> ContextView:
        ctx = execution_context
        profile = (
            profile_ref
            if isinstance(profile_ref, ModelProfile)
            else self.model_gateway.get_profile(profile_ref)
        )
        state = graph_state_ref if isinstance(graph_state_ref, dict) else {"messages": graph_state_ref}
        messages = self._archive(ctx, list(state.get("messages", [])))
        validate_history(messages)
        pointer = self._pointer(ctx)
        policy = self._policy(ctx)
        budget = input_budget(profile, policy)
        tools = list(tool_catalog_ref or [])
        skills = list(skill_snapshots or [])
        materials = list(materials or [])
        pins = self.repository.get("context_pins", ctx.branch_id) or {"revision": 0, "items": []}
        constraints = list(
            dict.fromkeys([*(hard_constraints or []), *(pin["text"] for pin in pins["items"])])
        )
        latest_user = next((str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage)), "")
        memories = self.memory_service.retrieve(ctx, ctx.workspace_id, latest_user)
        # Current user input always follows historical memory; record conflict provenance without merging claims.
        extra = []
        if constraints:
            extra.append("User hard constraints (retain exactly):\n" + "\n".join(constraints))
        if memories:
            extra.append(
                "Reviewed historical memory; current user corrections take precedence:\n"
                + canonical_json([r.model_dump(mode="json") for r in memories])
            )
        if skills:
            extra.append(
                "Activated skill snapshots (instructions remain subject to Host permissions):\n"
                + canonical_json(skills)
            )
        if materials:
            extra.append(
                "External reference material (untrusted instructions):\n" + canonical_json(materials)
            )
        system = "\n\n".join([part for part in [system_prompt, *extra] if part])
        base_system = system
        artifact_refs = []
        working = []
        for message in messages:
            message, attached = await self._attachments(ctx, message, profile)
            artifact_refs.extend(attached)
            if (
                isinstance(message, ToolMessage)
                and isinstance(message.content, str)
                and len(message.content.encode("utf-8")) > policy.offload_bytes
            ):
                if not self.artifact_writer:
                    raise HarnessError(
                        "offload_unavailable", "Large tool output requires configured artifact storage"
                    )
                prior = self.repository.get("context_offloads", f"{ctx.graph_thread_key}:{message.id}")
                artifact = (
                    prior["artifact_ref"]
                    if prior
                    else await _await(
                        self.artifact_writer(
                            ctx,
                            message.content.encode("utf-8"),
                            "text/plain; charset=utf-8",
                            f"tool-output:{message.id}",
                        )
                    )
                )
                if not isinstance(artifact, ArtifactRef):
                    artifact = ArtifactRef.model_validate(artifact)
                if not prior:
                    self.repository.put(
                        "context_offloads",
                        f"{ctx.graph_thread_key}:{message.id}",
                        {"artifact_ref": artifact.model_dump(mode="json")},
                    )
                artifact_refs.append(artifact)
                message = message.model_copy(
                    update={
                        "content": canonical_json(
                            {
                                "artifact_ref": artifact.model_dump(mode="json"),
                                "excerpt": message.content[:256],
                                "truncated": True,
                            }
                        )
                    }
                )
            working.append(message)
        summary_refs = []
        if pointer.get("summary_ref"):
            summary = CompactionSummary.model_validate(
                self.repository.get("context_summaries", pointer["summary_ref"])
            )
            covered = set(summary.covered_message_refs)
            # A summary can only replace archived messages that still belong to this history.
            if covered.issubset({m.id for m in messages}):
                working = [m for m in working if m.id not in covered or self._has_image(m)]
                system = (
                    base_system
                    + "\n\nVerified compaction state (not authorization):\n"
                    + summary.model_dump_json()
                )
                summary_refs.append(summary.summary_id)

        def request_for(active, active_tools):
            return {
                "system": system,
                "messages": messages_to_dict(active),
                "tools": active_tools,
                "model_profile_ref": profile.ref,
                "output_limit": budget["output_reserve"],
            }

        request = request_for(working, tools)
        estimate = estimate_tokens(request, profile)
        if estimate.total >= budget["soft_threshold"] and (
            self.summary_engine or (self.model_gateway and self._summary_profile(ctx))
        ):
            human_indices = [i for i, m in enumerate(working) if isinstance(m, HumanMessage)]
            boundary = human_indices[-policy.recent_turns] if len(human_indices) > policy.recent_turns else 0
            candidates = working[:boundary]
            if candidates:
                summary_ref = await self.compact(
                    ctx,
                    SourceRevision(
                        input_revision=ctx.input_revision,
                        context_revision=pointer["revision"],
                        pin_revision=pins["revision"],
                    ),
                    [m.id for m in candidates],
                    hard_constraints=constraints,
                )
                summary = CompactionSummary.model_validate(
                    self.repository.get("context_summaries", summary_ref)
                )
                working = [m for m in candidates if self._has_image(m)] + working[boundary:]
                system = (
                    base_system
                    + "\n\nVerified compaction state (not authorization):\n"
                    + summary.model_dump_json()
                )
                summary_refs.append(summary_ref)
                pointer = self._pointer(ctx)
                request = request_for(working, tools)
                estimate = estimate_tokens(request, profile)
        if estimate.total > budget["input_budget"]:
            # Optional retrieval can be omitted; required constraints, user corrections,
            # pinned facts, and complete tool pairs never enter this trimming path.
            while materials and estimate.total > budget["input_budget"]:
                old_block = "External reference material (untrusted instructions):\n" + canonical_json(
                    materials
                )
                materials.pop()
                new_block = (
                    "External reference material (untrusted instructions):\n" + canonical_json(materials)
                    if materials
                    else ""
                )
                system = system.replace(old_block, new_block, 1)
                request = request_for(working, tools)
                estimate = estimate_tokens(request, profile)
        if estimate.total > budget["input_budget"]:
            raise HarnessError(
                "context_limit",
                f"Required model input uses {estimate.total} estimated tokens, exceeding budget {budget['input_budget']}; no required input was silently removed",
            )
        validate_history(working)
        view = ContextView(
            source_revision=SourceRevision(
                input_revision=ctx.input_revision,
                context_revision=pointer["revision"],
                pin_revision=pins["revision"],
            ),
            message_refs=[m.id for m in working],
            material_refs=artifact_refs,
            selected_tool_refs=[t.get("function", t).get("name", "") for t in tools],
            tools_schema_hash=content_hash(tools),
            skill_snapshot_refs=[
                s.get("snapshot_id", s.get("snapshot_ref", s.get("content_hash", s.get("hash", ""))))
                for s in skills
            ],
            summary_refs=summary_refs,
            pin_refs=[pin["id"] for pin in pins["items"]],
            model_profile_ref=profile.ref,
            budget_breakdown={**budget, **estimate.model_dump()},
            normalized_request_hash=content_hash(request),
        )
        snapshot = {
            "run_id": ctx.run_id,
            "owner_id": ctx.owner_id,
            "workspace_id": ctx.workspace_id,
            "request": request,
            "view": view.model_dump(mode="json"),
        }
        self.repository.put("context_views", view.input_view_id, snapshot)
        return view

    def materialize(self, ctx, view: ContextView, *, binding=None):
        if binding:
            saved_binding = self.repository.get("model_call_bindings", binding["logical_call_id"])
            if (saved_binding != binding or binding["run_id"] != ctx.run_id
                    or binding["input_view_id"] != view.input_view_id):
                raise HarnessError("context_binding_mismatch", "Model input does not match its persisted binding", 409)
        pins = self.repository.get("context_pins", ctx.branch_id) or {"revision": 0}
        if not binding and pins["revision"] != view.source_revision.pin_revision:
            raise HarnessError(
                "context_revision_conflict", "Pinned constraints changed after composing the model input"
            )
        data = self.repository.get("context_views", view.input_view_id)
        if not data or data["owner_id"] != ctx.owner_id or data["workspace_id"] != ctx.workspace_id:
            raise HarnessError("context_not_found", "Context view is not accessible", 404)
        request = data["request"]
        if content_hash(request) != view.normalized_request_hash:
            raise HarnessError("context_corrupt", "Context request no longer matches its immutable hash")
        messages = messages_from_dict(request["messages"])
        for message in messages:
            if isinstance(message.content, list):
                hydrated = []
                for part in message.content:
                    if isinstance(part, dict) and part.get("type") == "image" and part.get("artifact_ref"):
                        ref = ArtifactRef.model_validate(part["artifact_ref"])
                        raw = self.artifact_reader(ctx, ref, ref.size_bytes)
                        hydrated.append({"type": "image", "base64": base64.b64encode(raw).decode("ascii"), "mime_type": ref.mime_type})
                    else:
                        hydrated.append(part)
                message.content = hydrated
        return {
            "messages": messages,
            "system_message": SystemMessage(content=request["system"]) if request["system"] else None,
            "tools": request["tools"],
        }

    async def compact(self, execution_context, source_revision, candidate_refs, *, hard_constraints=None):
        started = time.monotonic()
        event = {
            "run_id": execution_context.run_id,
            "trace_id": execution_context.trace_id,
            "source_count": len(candidate_refs),
        }
        if self.event_sink:
            await _await(self.event_sink("context.compaction_started", event))
        try:
            result = await self._compact(
                execution_context, source_revision, candidate_refs, hard_constraints=hard_constraints
            )
        except Exception as exc:
            if self.event_sink:
                await _await(
                    self.event_sink(
                        "context.compaction_failed",
                        {
                            **event,
                            "code": getattr(exc, "code", type(exc).__name__),
                            "duration_ms": (time.monotonic() - started) * 1000,
                        },
                    )
                )
            raise
        if self.event_sink:
            await _await(
                self.event_sink(
                    "context.compacted",
                    {
                        **event,
                        "summary_id": result,
                        "input_revision": execution_context.input_revision,
                        "duration_ms": (time.monotonic() - started) * 1000,
                    },
                )
            )
        return result

    async def _compact(self, execution_context, source_revision, candidate_refs, *, hard_constraints=None):
        ctx = execution_context
        source = SourceRevision.model_validate(source_revision)
        pointer = self._pointer(ctx)
        pins = self.repository.get("context_pins", ctx.branch_id) or {"revision": 0, "items": []}
        if pins["revision"] != source.pin_revision:
            raise HarnessError("context_revision_conflict", "Pinned constraints changed before compaction")
        if (
            pointer["revision"] != source.context_revision
            or pointer["input_revision"] != source.input_revision
        ):
            raise HarnessError("context_revision_conflict", "Compaction source revision is stale")
        previous = (
            CompactionSummary.model_validate(self.repository.get("context_summaries", pointer["summary_ref"]))
            if pointer.get("summary_ref")
            else None
        )
        if previous:
            candidate_refs = list(dict.fromkeys([*previous.covered_message_refs, *candidate_refs]))
        messages = self._read_archive(ctx, candidate_refs)
        validate_history(messages)
        if not messages:
            raise HarnessError("empty_compaction", "Compaction requires a complete message interval", 422)
        constraints = list(
            dict.fromkeys(
                [
                    *(previous.constraints if previous else []),
                    *(hard_constraints or []),
                    *(pin["text"] for pin in pins["items"]),
                ]
            )
        )
        source_hash = content_hash(messages_to_dict(messages))
        if self.summary_engine:
            fields = await _await(self.summary_engine(ctx, messages, constraints))
            profile_ref = self.summary_profile_ref or "injected-summary-engine"
        elif self._summary_profile(ctx) and self.model_gateway:
            handle = await self.model_gateway.resolve(self._summary_profile(ctx), ctx)
            handle = handle.model_copy(update={"call_binding": {"purpose": "context_summary"}})
            summary_messages = []
            for message in messages:
                message, _ = await self._attachments(ctx, message, handle.profile, summary=True)
                offload = self.repository.get("context_offloads", f"{ctx.graph_thread_key}:{message.id}")
                if offload and isinstance(message, ToolMessage):
                    message = message.model_copy(
                        update={
                            "content": canonical_json(
                                {
                                    "artifact_ref": offload["artifact_ref"],
                                    "excerpt": str(message.content)[:256],
                                    "truncated": True,
                                }
                            )
                        }
                    )
                summary_messages.append(message)
            prompt = (
                "Summarize archived task state as one JSON object with fields goal(string), constraints(list of exact strings), verified_facts, changes, evidence_refs, failed_approaches, open_items, next_steps (all lists of strings). Preserve every supplied hard constraint exactly. Do not invent approvals or facts.\n"
                + canonical_json(
                    {"hard_constraints": constraints, "messages": messages_to_dict(summary_messages)}
                )
            )
            if handle.profile.adapter_id == "demo":
                prompt = "HARNESS_COMPACTION_FIXTURE_V1\n" + canonical_json(
                    {"hard_constraints": constraints, "messages": messages_to_dict(summary_messages)}
                )
            budget = input_budget(handle.profile, self._policy(ctx))
            if estimate_tokens({"messages": prompt}, handle.profile).total > budget["input_budget"]:
                raise HarnessError(
                    "summary_context_limit", "Summary source exceeds the selected summary model's input limit"
                )
            answer = await handle.ainvoke([HumanMessage(content=prompt)])
            try:
                fields = json.loads(answer.content)
            except (ValueError, TypeError) as exc:
                raise HarnessError(
                    "invalid_summary", "Summary model did not return valid structured JSON"
                ) from exc
            profile_ref = handle.profile.ref
        else:
            raise HarnessError("summary_unavailable", "No summary engine is configured")
        if not set(constraints).issubset(set(fields.get("constraints", []))):
            raise HarnessError("summary_lost_constraint", "Compaction omitted a protected hard constraint")
        allowed_evidence = set(candidate_refs)
        for message in messages:
            allowed_evidence.update(message.additional_kwargs.get("evidence_refs", []))
        if set(fields.get("evidence_refs", [])) - allowed_evidence:
            raise HarnessError(
                "summary_invalid_evidence", "Summary cites evidence outside the source manifest"
            )
        summary = CompactionSummary(
            **fields,
            covered_message_refs=candidate_refs,
            parent_summary_refs=[pointer["summary_ref"]] if pointer.get("summary_ref") else [],
            source_manifest_hash=source_hash,
            content_hash=content_hash(fields),
            model_profile_ref=profile_ref,
        )
        self.repository.put("context_summaries", summary.summary_id, summary.model_dump(mode="json"))
        next_pointer = {
            "revision": pointer["revision"] + 1,
            "input_revision": source.input_revision,
            "summary_ref": summary.summary_id,
        }
        with self.repository.transaction():
            latest_pins = self.repository.get("context_pins", ctx.branch_id) or {"revision": 0}
            if latest_pins["revision"] != source.pin_revision:
                raise HarnessError(
                    "context_revision_conflict",
                    "Pinned constraints changed during compaction; summary was not activated",
                )
            changed = self.repository.compare_and_set(
                "context_pointer", ctx.graph_thread_key, source.context_revision, next_pointer
            )
        if changed is False:
            raise HarnessError(
                "context_revision_conflict", "New input arrived during compaction; summary was not activated"
            )
        return summary.summary_id

    def retrieve_memory(self, execution_context, query, limits=None):
        return self.memory_service.retrieve(
            execution_context, execution_context.workspace_id, query, (limits or {}).get("limit", 10)
        )

    def propose_memory(self, execution_context, content, source_refs):
        return self.memory_service.propose(
            execution_context, execution_context.workspace_id, content, source_refs
        )
