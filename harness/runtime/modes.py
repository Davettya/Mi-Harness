"""Host-owned mode policy. Model text and MCP annotations cannot grant capabilities."""

from harness.core import HarnessError

MODE_POLICY_VERSION = 1
PLAN_CAPABILITIES = {"file_read", "network", "input", "delegate", "mcp"}
PLAN_TOOLS = {
    "read_file",
    "stat_file",
    "list_files",
    "search_files",
    "fetch",
    "search_web",
    "artifact_read",
    "artifact_preview",
    "archive_search",
    "memory_search",
    "inspect_child",
    "request_input",
    "plan_update",
    "delegate",
    "wait_children",
    "skill_activate",
    "skill_read",
}
PLAN_PROMPT = (
    "\n当前模式 Plan：只读取证据、澄清需求和制定可审阅计划。禁止修改项目文件和执行命令。"
    "输出目标、现状依据、步骤、涉及文件、风险、验证方式和待确认问题；使用 plan_update 保存计划。"
    "宿主计划记录不是用户项目文件；计划确认不代替具体副作用审批。"
)


def permitted(mode, spec, snapshot=None):
    if mode != "plan":
        return True
    data = spec.model_dump() if hasattr(spec, "model_dump") else spec
    # MCP readOnlyHint is untrusted. Only an independent host allowlist can admit it.
    if data.get("origin", "builtin").startswith("mcp:"):
        return data.get("effect") == "read" and data.get("id") in (snapshot or {}).get(
            "plan_readonly_tools", []
        )
    return (
        data.get("origin", "builtin") == "builtin"
        and data.get("effect", "read") == "read"
        and data.get("id") in PLAN_TOOLS
    )


def enforce(snapshot, spec):
    if not permitted(snapshot.get("mode", "react"), spec, snapshot):
        raise HarnessError("PLAN_TOOL_DENIED", "Plan 模式禁止此工具；请选择 ReAct 发起新运行", 403)
