# MCP 与 Agent Skills 调研及集成设计

查阅日期：2026-09-17。范围：通用本地 Agent Harness 的扩展接入层；结论来自当日官方在线文档。本文件没有安装依赖、运行 SDK 互操作测试或验证第三方 MCP Server。文中的“规范要求”“文档事实”和“设计建议”分别代表标准条款、库文档能力与本项目拟采用方案。

## 1. 必须写入总设计的版本结论

**不能把 MCP 2025-11-25 的握手和会话语义直接当成当前协议。** 当日打开 `https://modelcontextprotocol.io/specification/latest` 重定向到 `2026-07-28`。官方 Versioning 页明确将 `2026-07-28` 标为 **Current**，解释为可使用的当前版本；Draft 是另一个类别。协议日期表示最近一次不兼容修订日期，并非 SDK 版本号。[MCP 当前版本说明](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning)

同时，LangChain MCP 接入刚发生迁移：官方 2026-09-03 公告改用内置 `langchain.mcp`，底层由 FastMCP 处理传输、协商与认证，支持新旧协议时代。[LangChain 公告](https://www.langchain.com/blog/mcp-in-langchain-stateless-protocol-elicitation-and-more)

| 层次 | 截至查阅日核实结果 | 不能据此推定的内容 |
|---|---|---|
| MCP 规范 | `2026-07-28` 为 Current | 生态内所有 Server 已升级 |
| 官方 MCP Python SDK | README 声明 v2 为当前稳定分支，支持 2026-07-28 及以前修订；v1 分支继续关键修复 | 任意旧 Python 应用直接升级 v2 无破坏性变化 |
| LangChain | `langchain[mcp]>=1.4.0` 提供 `MCPAdapter`；此 namespace 仍为 beta | `MCPAdapter` 覆盖所有 MCP primitives/扩展 |
| 旧适配包 | `langchain-mcp-adapters` 仓库标注不再积极维护，指引迁移 `langchain.mcp` | 旧包当日已不可用；本调研未作此结论 |
| Agent Skills | SKILL.md 文件格式标准和 client 实现指南均可用 | 通用统一的安装器、审批 DSL、脚本沙箱已由标准规定 |

依据：[Python SDK README](https://github.com/modelcontextprotocol/python-sdk)、[LangChain MCP 总览](https://docs.langchain.com/oss/python/langchain/mcp)、[旧适配包迁移声明](https://github.com/langchain-ai/langchain-mcp-adapters)、[Agent Skills 规范](https://agentskills.io/specification)。

**设计建议：** 对外设计稳定的 `McpGateway`，将 beta API 封装在一个实现模块内。立项 PoC 首选 `langchain.mcp.MCPAdapter + FastMCP`，锁定完整依赖集与哈希，跑新旧协议验收后才形成生产基线；不在未安装和测试时承诺某个补丁版本可投产。若 beta 组合未达验收，可用官方 SDK 实现同一个 Gateway 接口，或临时锁定旧兼容栈作为有退出计划的过渡。不要将全部工具逻辑绑定到旧 `MultiServerMCPClient`。

## 2. 协议兼容矩阵

| 议题 | 2025-11-25 及较早兼容档 | 2026-07-28 当前档 |
|---|---|---|
| 初始化 | `initialize` 请求→服务端版本/能力→`notifications/initialized`；只调用已协商能力 | 不再有该握手。每个请求携带版本和 client capabilities；`server/discover` 为服务端必须实现、客户端可选的发现方法 |
| HTTP 会话 | 服务端可赋予 `Mcp-Session-Id`；有需要时后续请求携带，DELETE 结束 | 移除协议会话和此 Header；跨调用业务状态通过工具返回并接收的显式 handle 实现 |
| Streamable HTTP | POST/GET，同一 MCP endpoint；GET 可供独立服务器消息流；可有 SSE 恢复 | 单 endpoint 接收 POST；JSON 或请求级 SSE 响应；移除 GET 流和 Last-Event-ID 恢复 |
| stdio | 子进程标准流、逐行 JSON-RPC，先握手 | 保留逐行 JSON-RPC 和进程生命周期，请求内 `_meta` 协商，不能发送服务器主动 JSON-RPC request |
| `list_changed` | 能力协商后使用列表变化通知 | 能力仍可声明；通过 `subscriptions/listen` 主动选择所需变化通知并建立长连接响应流 |
| 用户补充输入 | 服务端主动 elicitation request | `resultType: input_required` 与 MRTR；客户端补充 inputResponses，再发起原请求 |
| 取消 | 请求取消通知；断流语义需依原兼容档执行 | HTTP 关闭当前 SSE 响应流即取消；stdio 发 `notifications/cancelled` |
| 长任务 | tasks 属实验性核心能力 | tasks 移到独立可选扩展；不要当作 Harness 调度器 |

主要依据：[2025-11-25 Lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[2025-11-25 Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[2026-07-28 Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)、[当前 Transports Overview](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports)、[当前 HTTP 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[当前 stdio 规范](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)。

**容易混淆的四种状态必须分表、分名称：**

1. `conversation_id / thread_id`：Harness 用户业务会话与 LangGraph checkpoint 命名空间。
2. `run_id / tool_execution_id`：一次执行及工具尝试，负责重放、审计、计费、取消。
3. `connection_id / legacy_mcp_session_id`：传输资源与旧版 MCP 协议会话；禁止用作业务会话主键。
4. `remote_handle`：远端业务对象、浏览器上下文或任务句柄；需绑定 server、认证主体、有效期和用途。重建网络连接并不等于该对象恢复或丢失。

SDK 的 `session` 或 context manager 名称仍可能存在；这只是连接/对象生命周期术语，不能据此声称现代 MCP 又有协议会话。

## 3. MCP 接入功能与技术边界

### 3.1 用户功能

建议 Web 设置页展示 MCP Server 列表：名称、来源、协议时代、传输方式、连接健康、认证状态、可用工具/资源/提示词数量、最近失败原因。支持增加、导入、启停、重新认证、检查配置和手动刷新。

新增 Server 的可审阅对象应包括：本地可执行文件和参数或远端 URL、需要的环境变量名称、工作目录、作用项目、网络目的地、拟开放能力。配置导入不自动启动陌生子进程，连接成功也不自动执行任意工具。

工具调用时间线应显示 server 来源、标准化工具名、参数摘要、授权依据、开始与完成时间、取消结果、错误分类；完整输出按需展开或作为产物保存。秘密值只显示引用名，不显示明文。

### 3.2 Primitives 与 LangChain 的映射

| MCP 对象 | Harness 层的职责 | 给模型/用户的入口 |
|---|---|---|
| Tools | 将 JSON Schema 适配为 LangChain tool；保留 server 原始名称、schema hash、内容类型和可信来源 | 模型选工具→统一 Tool Executor 授权→Gateway 执行 |
| Resources / resource templates | 可发现、按 URI 读取、分页/限流；按用户与项目隔离缓存 | 上下文选择器、`read_resource`；内容默认当作外部数据 |
| Prompts | 获取参数化提示模板，保留来源，供用户显式选择 | Web 命令菜单；不自动把服务端 prompt 提升为系统指令 |
| Elicitation | 生成可持久化的人类输入请求，关联当前工具尝试与 run | LangGraph interrupt→Web 表单→resume；用户可拒绝/取消 |
| 可选扩展 | 通过单独能力开关和版本支持矩阵启用 | 不支持时明确报告，不能伪装执行成功 |

`langchain.mcp` 当前聚焦 tools，没有 prompts/resources wrapper；需要直接调用 FastMCP Client 的 `get_prompt`、`read_resource`。它不回答 sampling/roots 请求；这类请求可能抛 `NotImplementedError`。工具执行 `isError=True` 映射为 `ToolMessage(status="error")`，传输失败则抛异常。上述是适配库边界，不等同于协议没有这些能力。[官方迁移指南](https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters)

现代协议输出可以是任意 JSON 值的 `structuredContent`，它和模型的 JSON structured output 是两回事；需保存与验证工具输出 schema。业务错误和协议错误分开，错误不能都扁平化为“网络异常”。[MCP Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

### 3.3 Gateway 模块契约（建议）

```text
McpGateway
  inspect_server(server_ref) -> ServerInspection
  discover(server_ref, principal, cache_policy) -> CatalogSnapshot
  list_resources / read_resource / list_prompts / get_prompt
  invoke(ToolCallEnvelope) -> ToolOutcome | HumanInputRequired
  cancel(tool_execution_id) -> CancelOutcome
  watch_catalog(server_ref, principal) -> catalog events
  close_scope(project_id, run_id)

ToolCallEnvelope
  run_id, tool_execution_id, principal_id, project_id
  server_id, original_tool_name, schema_hash, arguments
  deadline, policy_decision_id, correlation_id

ToolOutcome
  status: success | tool_error | protocol_error | transport_error
        | cancelled | timeout | unknown_outcome
  model_content, structured_content, artifact_refs
  protocol_version, server_identity, duration, retry_count
```

该契约属于 Harness 内部接口，不发明 MCP 线协议方法。协议字段由 SDK 处理；`tool_execution_id` 不冒充 MCP JSON-RPC request id。

连接建议每个 Server、认证主体和执行环境独立管理。多 Server 使用 `ClientGroup` 或每个 Server 单独 adapter，避免一个旧服务让整个 aggregate `MCPConfig` 降到 legacy era。保持 adapter context 覆盖同一 run 可以复用底层连接；默认工具在每次调用进入/退出 client。连接复用与用户状态隔离必须分别设计。[LangChain Connections](https://docs.langchain.com/oss/python/langchain/mcp/connections)

本地 stdio 子进程须受进程管理器监督：明确 `command + args`，避免字符串拼接 shell；stdin/stdout 只跑协议，stderr 单独限量采集；Windows 用 Job Object 约束进程树及退出，POSIX 使用进程组。保留 5 秒优雅退出预算和强制回收上限是本项目建议，不是协议规定。实际超时值应经慢工具验收后配置。

## 4. 发现、缓存、取消与重试

**规范要求/文档事实：** 分页 cursor 是不透明字符串；不能解析，也不能把空字符串 cursor 当“没有下一页”。`nextCursor` 缺失或 null 才结束。tools/resources/prompts 等 list 均可分页。[MCP Pagination](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/pagination)

现代版本提供 `ttlMs`、`cacheScope` 缓存提示；private 缓存不能跨授权上下文，MRTR 带 `inputResponses/requestState` 的结果不能缓存。通知会使缓存立即失效；分页不保证跨页快照一致性。[MCP Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)

**本项目建议：** Catalog Snapshot 键为 `server_id + principal_fingerprint + protocol_version + config_revision`；保存 schema digest，在每个 run 起点选择一次可重复的工具目录。收到变化通知先使缓存失效，再刷新下一轮工具候选；如果当前计划要执行的工具 schema 已变，先重新验证参数和权限，禁止静默执行旧 schema 的写操作。

建议设置分页上限、总工具数上限、重复 cursor 检测和总响应字节上限。单个恶意或异常 Server 不应阻塞全部目录。大目录使用“搜索/选择工具→加载完整 schema”的二阶段暴露，工具名缩短时仍保存原始映射，禁止名称冲突时覆盖。

**取消规范：** 当前 HTTP 断开请求响应流表示取消，stdio 用取消通知；应设每请求超时和不可无限延长的总上限。取消不代表已经产生的外部副作用被撤销。[MCP Cancellation](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation)

| 失败种类 | 建议处理 | 自动重试边界 |
|---|---|---|
| 连接前失败 / DNS / 明确请求未发送 | 指数退避加随机抖动、Server 级熔断 | 有上限；不无限尝试 |
| 429 / 可恢复 503 | 识别 Retry-After，检查预算和总 deadline | 仅在已知幂等或明确未执行时自动重试 |
| 写操作已发送后断流 / timeout | 标记 `unknown_outcome`，查远端操作状态或询问用户 | 无 idempotency 契约不得自动重复发送 |
| `isError=True` 业务错误 | 给模型简短可行动错误，原始细节存 artifact | 参数修正后计作新尝试；限制循环 |
| schema / method not found | 刷新目录并报告变更 | 不在未重审权限时直接执行替代工具 |
| 401 | 触发认证恢复；从当前 checkpoint 暂停 | 有界刷新，防止循环登录 |
| 403 scope 不足 | 请求可解释的增量授权 | 用户拒绝后停止该操作 |
| 用户取消 | 传播到正在运行子工具、停止新调度 | 不自动恢复已取消操作 |

当前协议断流后若再发请求，需要新 request id；这不提供“恰好一次”执行保证。应将网络级重发与业务级重试分开记账。内置写工具实现本地操作账本与幂等键；第三方工具只有在其公开契约或经过配置确认后才采用幂等重试。

## 5. OAuth 与本地安全

**规范要求：** MCP HTTP 授权为可选能力，采用时遵循 OAuth 规范；stdio 不使用这套 HTTP OAuth 流程，可从环境取得凭据。HTTP 客户端使用 Protected Resource Metadata 发现授权服务；发送适当 resource；Token 绑定目标服务，不放 URL 查询参数。当前规范中 Dynamic Client Registration 已弃用但保留兼容路径。[MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)

OAuth 客户端需 PKCE，能用时采用 S256；校验 issuer/redirect/state 相关要求，安全保存 token，不将给一个 MCP Server 的 token 透传给另一个上游。授权服务器 endpoint 需 HTTPS，redirect URI 可为 localhost。[Authorization Security](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/security-considerations)

MCP HTTP Server 必须校验 Origin；本地服务建议只绑定 loopback，并实施认证。[Streamable HTTP 安全要求](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)

**本项目建议：**

- Harness Web 与 API 默认同源、仅 loopback，启动随机认证 secret，经一次性启动链接兑换 HttpOnly 会话 Cookie；并实施 Origin、Host、CSRF 校验。跨机器访问是独立部署模式，显式配置 TLS 与身份认证，不默认 `0.0.0.0`。
- OAuth 全流程由本地服务持有凭据、state 与 PKCE verifier；浏览器只承接授权 UI。系统钥匙串存储 token，配置只保存 secret ref；日志、checkpoint 和 LLM 输入不得包含秘密。
- 权限取交集：用户当前授权 ∩ 项目策略 ∩ agent 能力 ∩ tool 风险策略。Server 声明的 `readOnlyHint` 等 annotations 是提示，不能单独授予权限；陌生 server 的名称/描述/结果也是不可信输入。
- 任意 URL 的 MCP 连接与 OAuth 元数据读取应走 SSRF 约束；允许显式配置的 loopback 服务不等于允许对所有内网、云 metadata 地址任意访问。对跳转目的地重新验证。
- 进程环境采用 allowlist，给各 Server 注入专用秘密；不继承整个父进程环境。Tool/Skill 代码在执行器内统一限制工作目录、可写路径、网络、CPU/内存/时间；文档检索内容不能修改这些策略。
- 手动暂停和关闭 UI 不是同一动作：页面断线只断事件订阅，run 是否继续由产品设置决定；点击“取消”才发业务取消命令。避免 Web SSE 断线意外取消远端 MCP。

## 6. Agent Skills 格式与执行模型

**格式事实：** 一个 skill 目录至少含 `SKILL.md`，由 YAML frontmatter 与 Markdown 正文构成；必填 `name`、`description`，可含 license、compatibility、metadata；`allowed-tools` 是实验字段，不能视为跨产品一致的权限语言。name/description 有长度与字符约束。`scripts/`、`references/`、`assets/` 是常用约定目录。加载分 metadata、完整指令和按需资源三个层次。[Agent Skills Specification](https://agentskills.io/specification)

**官方实现指南事实：** `.agents/skills` 是跨客户端常用约定，而非规范强制安装路径；建议项目级和用户级发现、冲突时确定性优先级、明确项目可信性、激活去重和保护已激活指令不被上下文压缩随意丢弃。专用 activation tool 和普通文件读取均可实现激活。[Adding Skills Support](https://agentskills.io/client-implementation/adding-skills-support)

**本项目建议的数据模型：**

```text
SkillRecord:
  skill_id = source_id + relative_path
  name, description, root_uri, content_hash, enabled
  scope = built_in | user | project | remote
  source_url, source_revision, license, compatibility
  trust_state, validation_diagnostics, dependency_metadata

SkillActivation:
  conversation_id, run_id, agent_id, skill_id, content_hash
  activated_by = user | model
  loaded_resource_refs, activated_at, active_until
```

`metadata` 内私有命名空间可记录 Harness 的扩展字段；不要更改标准必填语义。`allowed-tools` 仅作为拟请求的工具集合，真实许可仍由 Harness 策略决策。作者写“预先允许 Bash”不能扩大项目权限。Skill 不是可执行入口本身；模型读完说明后调用执行器执行指定脚本，所有实际副作用仍须走 Tool Executor。

建议工作流：

1. **发现**：内置、用户目录、项目目录及显式添加路径；有限深度扫描，跳过依赖/构建目录；符号链接和路径规范化后执行边界检查。
2. **索引**：安全 YAML parser；验证 name/description，记录诊断；非法解析或缺少 description 则禁用。严格校验用于发布，兼容导入只在 UI 明示警告后允许非安全性格式差异。
3. **目录披露**：会话开始仅注入 `name + description + skill_id`；按项目、信任与权限过滤。目录过大采用可搜索索引并记录这是 Harness 扩展策略。
4. **激活**：用户 `/skill` 或模型 `activate_skill(skill_id)`；返回带来源/哈希/根目录的正文及有限资源目录，不自动读取所有文件。
5. **资源读取**：相对引用解析到 skill root；读取资产可预授权，执行脚本单独按操作风险判断。拒绝逃逸引用、危险 archive 路径、压缩炸弹；无需将“读文件许可”和“执行代码许可”混为一谈。
6. **上下文管理**：状态保存 active skill IDs/hashes。压缩时保留仍适用的激活指令，或压缩后按同一哈希重新加载；超预算应显示退激活/切分任务，而不是静默丢规则。阶段结束可主动退激活。
7. **更新**：安装版本不可变、run 使用哈希快照；新版本在下一 run 生效。来源更新与脚本变更可审阅，不让运行中任务突然换一份规则。

推荐 UI 提供启停、来源、权限、验证诊断、手动激活、查看正文、版本/哈希、当前使用状态。导入 skill 不代表安装其依赖、运行安装脚本或允许联网。

## 7. Skills over MCP：单列扩展，不承诺全生态可用

截至 2026-09-17，官方 SEP-2640 PR 已于 **2026-09-13 merged**，其最终方向是基于 resources 的 `skill://` 约定与 `io.modelcontextprotocol/skills` 扩展标识。已有参考实现链接，但不能据此认定所有 SDK 的已发布版本均支持。[SEP-2640 PR](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2640)、[官方工作组说明](https://modelcontextprotocol.io/community/working-groups/skills-over-mcp)、[扩展仓库](https://github.com/modelcontextprotocol/ext-skills)

设计建议 MVP 先实现本地 SKILL.md，P1 单列远端 Skills 扩展适配器。远端 skill 转为同一 `SkillRecord`，但 root 使用 provider URI，绝不当作本地文件路径。获取资源、archive 安全解包、完整性、来源变更、能力发现和权限应分别测试；缺少扩展声明时不能猜测私有方法名并假装标准兼容。

## 8. 验收用例与完成条件

以下均为设计验收目标，本次尚未执行。

| ID | 场景 | 通过标准 |
|---|---|---|
| MCP-01 | 本地 stdio modern server | 发现、调用、取消、退出；无 stdout 日志污染；子进程不残留 |
| MCP-02 | stdio legacy server 不识别 discover | 有界探测后进入 initialize；记录真实 negotiated version |
| MCP-03 | modern HTTP 只提供 POST | 调用不依赖 GET、Mcp-Session-Id、Last-Event-ID；接受 JSON 和 SSE |
| MCP-04 | legacy HTTP server | 正确持有服务端 session 与恢复/终止规则；业务 thread 不受 session ID 变化影响 |
| MCP-05 | 同一 agent 混用新旧 Server | 独立协商；不会让 modern server 被无声整体降级 |
| MCP-06 | 分页包含空字符串 cursor / 重复 cursor | 前者继续分页；后者有界终止并给诊断；无遗漏被误报“完整” |
| MCP-07 | list_changed 与工具 schema 变化 | 目录失效、参数重新验证，旧权限不能绑定新行为 |
| MCP-08 | 两个认证主体读取同 URI | private cache 隔离，无跨用户内容泄漏 |
| MCP-09 | tools 返回 isError、协议错误、传输错误 | 三种状态可区分；模型获得可修正错误，系统可重试错误不被混淆 |
| MCP-10 | 写入完成但响应流断开 | 展示 unknown_outcome；无契约时不自动重复写 |
| MCP-11 | 现代 elicitation + 服务重启 | interrupt 持久化，Web 拒绝/取消/提交正确路由；重放不重复副作用 |
| MCP-12 | OAuth issuer 不匹配、scope 不足、token 过期 | 前者拒绝；后两者有界恢复；token 不进入日志和提示词 |
| MCP-13 | 恶意 Origin / DNS rebinding / 任意网页访问 localhost | Host/Origin/认证校验阻止非授权请求 |
| MCP-14 | roots/sampling/未知扩展 | 明确 Unsupported；不错误广告支持 |
| SKILL-01 | 标准 SKILL.md 与异常 YAML | 标准正确索引；诊断可见，失效条目不污染目录 |
| SKILL-02 | 50 个 skill，激活其中一个 | 初始不加载全部正文；只加载被选指令和实际需要的资源 |
| SKILL-03 | 冲突名称、未信任项目 | 确定性解决且可见；项目内容不能静默取得执行权 |
| SKILL-04 | 脚本要求越权 / allowed-tools 宣称全权 | 策略拒绝，Skill 无法自我授权 |
| SKILL-05 | 压缩和恢复 | 已激活 skill 与来源哈希保持，脚本执行记录可审计 |
| SKILL-06 | archive path traversal、超大资源、更新竞态 | 解包/读取被限制；旧 run 保持原哈希快照 |
| SKILL-07 | 远程 skill 只提供 URI | 经 Provider 读取，不把 URI 当本地路径；来源不可变并可追踪 |

集成的完成条件应写为“指定 SDK/依赖锁文件 + 两个协议档位 + 两种传输 + 认证/分页/取消/失败注入用例全部通过”，而不是“成功列出一个 MCP 工具”。本次文档调查证明的是协议和官方接口状态，不能替代这些运行验证。
