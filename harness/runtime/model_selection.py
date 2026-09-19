"""Persistent preferences, commands and fenced logical-call bindings; no model loop."""

from harness.core import HarnessError, content_hash, new_id, utc_now


class ModelSelectionService:
    def __init__(self, services):
        self.services, self.store = services, services.store

    def preferences(self, owner, session_id):
        session = self.store.session(session_id, owner)
        with self.store.transaction():
            if session["agent_spec_id"] != "default" and not self.store.get(
                "session_legacy_bounds", session_id
            ):
                original = self.store.get("config/agents", session["agent_spec_id"])
                if original:
                    access = type(
                        "Access", (), {"workspace_id": session["workspace_id"], "owner_id": owner}
                    )()
                    self.store.put(
                        "session_legacy_bounds",
                        session_id,
                        dict(
                            agent_spec=original,
                            policy=self.services.policy.for_agent(access, original["policy"]),
                        ),
                    )
            saved = self.store.get("session_preferences", session_id)
            if saved:
                return saved
            agent = self.store.get("config/agents", session["agent_spec_id"]) or {}
            saved = dict(
                session_id=session_id,
                revision=1,
                mode="react",
                model_profile_ref=agent.get("model_policy", {}).get("profile_ref"),
                migration="frozen_original_agent",
                legacy_agent_id=session["agent_spec_id"],
            )
            self.store.compare_and_set("session_preferences", session_id, None, saved)
            return saved

    def validate(self, owner, ref, *, images=False):
        pinned = self.services.settings.model_profile_ref
        if pinned and pinned != ref:
            raise HarnessError("MODEL_PROFILE_PINNED", "启动配置固定了模型", 409)
        from harness.model_gateway.profiles import GatewayError

        try:
            profile = self.services.models.get_profile(ref)
        except (GatewayError, TypeError):
            raise HarnessError("MODEL_UNAVAILABLE", "所选模型版本不可用，请重新选择", 422) from None
        if profile.ref != ref:
            raise HarnessError("MODEL_REFERENCE", "模型选择必须包含固定版本", 422)
        if not profile.supports("text") or not profile.supports("tool_calling"):
            raise HarnessError("MODEL_UNVERIFIED", "模型尚未通过文本与工具协议验证", 422)
        if images and not profile.supports("vision"):
            raise HarnessError("VISION_UNVERIFIED", "当前模型配置尚未完成图片能力验证；请在设置 → 模型中验证并保存，再选择新版本。这不代表模型不支持图片。", 422)
        ctx = self.services.diagnostic(owner, ref)
        self.services.check_model_policy(ctx, profile)
        return profile

    def available(self, owner, session_id=None):
        images = False
        if session_id:
            session = self.store.session(session_id, owner)
            images = any(
                self.has_images(m.get("content_parts", []))
                for m in self.store.messages(session["default_branch_id"])
            )
        items = []
        pinned = self.services.settings.model_profile_ref
        for p in self.store.list("config/models"):
            # The deterministic Demo is an internal protocol/test fixture.  Keep
            # its immutable profile available for historical snapshots, but do
            # not project it into the product model catalog.
            if p.get("adapter_id") == "demo" or p.get("profile_id") == "demo":
                continue
            ref = f"{p['profile_id']}@{p['revision']}"
            caps = p.get("capabilities", {})
            verified = lambda name: caps.get(name, {}).get("status") == "verified"
            reason = (
                "受启动配置固定"
                if pinned and pinned != ref
                else "文本或工具能力待验证"
                if not (verified("text") and verified("tool_calling"))
                else "当前对话包含图片；视觉能力待验证"
                if images and not verified("vision")
                else None
            )
            items.append(
                dict(
                    profile_ref=ref,
                    provider_id=p["provider_id"],
                    model_id=p["model_id"],
                    capabilities=caps,
                    disabled_reason=reason,
                )
            )
        default = self.store.get("config/agents", "default")["model_policy"]["profile_ref"]
        visible_refs = {item["profile_ref"] for item in items}
        default_ref = pinned or default
        if default_ref not in visible_refs:
            default_ref = next(
                (item["profile_ref"] for item in items if item["disabled_reason"] is None),
                items[0]["profile_ref"] if items else None,
            )
        return dict(
            items=items,
            pinned_profile_ref=pinned if pinned in visible_refs else None,
            default_profile_ref=default_ref,
        )

    @staticmethod
    def has_images(parts):
        return any(
            isinstance(p, dict) and p.get("type") in {"image", "image_url", "input_image"} for p in parts
        )

    def update_preferences(self, owner, session_id, body):
        with self.store.transaction():
            old = self.preferences(owner, session_id)
            if old["revision"] != body["expected_revision"]:
                raise HarnessError("REVISION_CONFLICT", "会话偏好已更新；保留选择并刷新", 409)
            ref = body.get("model_profile_ref") or old["model_profile_ref"]
            self.validate(owner, ref)
            record = {
                **old,
                "revision": old["revision"] + 1,
                "mode": body.get("mode") or old["mode"],
                "model_profile_ref": ref,
            }
            self.store.compare_and_set("session_preferences", session_id, old["revision"], record)
            return record

    def control(self, run_id):
        run = self.store.run(run_id)
        saved = self.store.get("run_model_controls", run_id)
        if saved:
            return saved
        snap = self.store.get("snapshots", run["snapshot_id"])
        ref = snap["agent_spec"]["model_policy"]["profile_ref"]
        return dict(
            run_id=run_id,
            control_revision=0,
            effective_revision=0,
            desired_profile_ref=ref,
            effective_profile_ref=ref,
            status="initial",
        )

    def select(self, owner, run_id, body):
        if not self.services.settings.hot_model_selection:
            raise HarnessError("MODEL_SELECTION_DISABLED", "当前启动配置关闭运行中模型切换", 409)
        run = self.store.run(run_id, owner)
        images = any(
            self.has_images(m.get("content_parts", [])) for m in self.store.messages(run["branch_id"])
        )
        self.validate(owner, body["model_profile_ref"], images=images)
        with self.store.transaction():
            run = self.store.run(run_id, owner)
            branch = self.store.branch(run["branch_id"])
            if run["status"] in {"completed", "failed", "cancelled", "cancelling"} or run["cancel_requested"]:
                raise HarnessError("RUN_STOPPED", "运行已结束或正在停止；只可修改后续偏好", 409)
            if branch.get("active_run_id") not in {None, run_id} and run["status"] != "queued":
                raise HarnessError("RUN_BRANCH_CONFLICT", "指定运行不是当前分支的活动任务", 409)
            old = self.control(run_id)
            if old["control_revision"] != body["expected_control_revision"]:
                raise HarnessError("REVISION_CONFLICT", "模型选择已更新，请刷新后重试", 409)
            if body.get("persist_for_session"):
                if body.get("expected_preferences_revision") is None:
                    raise HarnessError("PREFERENCES_REVISION_REQUIRED", "同时保存会话偏好需要版本", 422)
                self.update_preferences(
                    owner,
                    run["session_id"],
                    dict(
                        model_profile_ref=body["model_profile_ref"],
                        expected_revision=body["expected_preferences_revision"],
                    ),
                )
            if old.get("command_id") and old["status"] == "requested":
                previous = self.store.get("run_model_commands", old["command_id"])
                self.store.put("run_model_commands", old["command_id"], {**previous, "status": "superseded"})
                self.store.event(run_id, "model.selection.superseded", {"command_id": old["command_id"]})
            identity, revision = new_id(), old["control_revision"] + 1
            command = dict(
                command_id=identity,
                run_id=run_id,
                control_revision=revision,
                profile_ref=body["model_profile_ref"],
                owner_id=owner,
                accepted_at=utc_now(),
                status="requested",
            )
            self.store.put("run_model_commands", identity, command)
            current = {
                **old,
                "command_id": identity,
                "control_revision": revision,
                "desired_profile_ref": body["model_profile_ref"],
                "status": "requested",
            }
            self.store.put("run_model_controls", run_id, current)
            self.store.event(run_id, "model.selection.requested", current)
            return current

    def logical_id(self, ctx, messages, step):
        return content_hash(
            [ctx.run_id, ctx.input_revision, step, [m.model_dump(mode="json") for m in messages]]
        )

    def candidate(self, ctx, logical_id):
        self.store.assert_fence(ctx)
        binding = self.store.get("model_call_bindings", logical_id)
        control = self.control(ctx.run_id)
        return binding or dict(
            logical_call_id=logical_id,
            run_id=ctx.run_id,
            purpose="agent",
            control_revision=control["control_revision"],
            profile_ref=control["desired_profile_ref"],
        )

    def bind(self, ctx, candidate, view):
        with self.store.transaction():
            self.store.assert_fence(ctx)
            old = self.store.get("model_call_bindings", candidate["logical_call_id"])
            if old:
                return old
            control = self.control(ctx.run_id)
            if candidate["control_revision"] != control["control_revision"]:
                return None
            binding = {
                **candidate,
                "input_view_id": view.input_view_id,
                "fencing_token": ctx.fencing_token,
                "bound_at": utc_now(),
            }
            self.store.compare_and_set("model_call_bindings", candidate["logical_call_id"], None, binding)
            current = {
                **control,
                "effective_profile_ref": candidate["profile_ref"],
                "effective_revision": candidate["control_revision"],
                "status": "applied",
            }
            self.store.put("run_model_controls", ctx.run_id, current)
            if control.get("command_id"):
                command = self.store.get("run_model_commands", control["command_id"])
                self.store.put(
                    "run_model_commands",
                    control["command_id"],
                    {**command, "status": "applied", "logical_call_id": candidate["logical_call_id"]},
                )
            self.store.event(ctx.run_id, "model.selection.applied", current)
            return binding

    def reject(self, ctx, candidate, code):
        with self.store.transaction():
            self.store.assert_fence(ctx)
            control = self.control(ctx.run_id)
            if candidate["control_revision"] != control["control_revision"]:
                return
            current = {**control, "status": "rejected", "reason": code}
            self.store.put("run_model_controls", ctx.run_id, current)
            if control.get("command_id"):
                command = self.store.get("run_model_commands", control["command_id"])
                self.store.put(
                    "run_model_commands",
                    control["command_id"],
                    {**command, "status": "rejected", "reason": code},
                )
            self.store.event(ctx.run_id, "model.selection.rejected", current)

    def finish(self, run):
        control = self.control(run["id"])
        if control["status"] == "requested":
            current = {**control, "status": "not_applied"}
            self.store.put("run_model_controls", run["id"], current)
            command = self.store.get("run_model_commands", control["command_id"])
            self.store.put("run_model_commands", control["command_id"], {**command, "status": "not_applied"})
            self.store.event(run["id"], "model.selection.not_applied", current)
