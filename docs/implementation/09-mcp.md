# 09 · MCP Gateway 实现指导

[实现索引](README.md) · [公共契约](01-contracts.md) · 前置：[工具执行](07-tools.md)、[权限与部署](12-platform.md)

本文唯一维护 MCP connection profile、Gateway 接口、协议兼容和连接生命周期。
工具账本、操作幂等性和副作用结果核对归 07；人工等待与恢复归 05/06；物理存储归 [02](02-storage.md)。

## 1. 分期范围

P0 支持 tools、stdio、Streamable HTTP、显式旧协议兼容及配置凭据；所有 Agent 工具调用经过 Tool Gateway。
P0 对 elicitation 有界拒绝，明确不广告 sampling、roots 或未经实现的扩展能力。
P1 增加 Resources/Prompts、完整 OAuth、elicitation 表单桥接和逐项验收通过的可选扩展。
原调研采用 `langchain.mcp.MCPAdapter + FastMCP`；实施前以锁版本互操作结果确定基线，不假定 beta API 签名稳定。

## 2. 模块与专用 DTO

| 模块 | 职责 |
|---|---|
| `packages/mcp_gateway/profiles.py` | 配置校验、凭据引用、能力开关 |
| `packages/mcp_gateway/client_factory.py` | 为每个隔离域建立 FastMCP client，注入 handler |
| `packages/mcp_gateway/adapter.py` | 唯一引用 beta `MCPAdapter` API 的封装 |
| `packages/mcp_gateway/catalog.py` | 分页发现、缓存、别名映射、schema 快照 |
| `packages/mcp_gateway/invocation.py` | 调用、取消、结果和错误映射 |
| `packages/mcp_gateway/diagnostics.py` | 分层诊断与能力实测报告 |

```text
ConnectionProfile
  server_id, config_revision, display_name
  transport: stdio | streamable_http | legacy_sse
  command?, args[]?, cwd_ref?, endpoint?
  environment_refs{}, credential_ref?, allowed_capabilities[]
  protocol_policy: auto | modern_only | legacy_only
  connection_scope: call | run
  connect_timeout, request_timeout, max_total_duration

CatalogSnapshot
  server_id, principal_fingerprint, config_revision, protocol_version
  discovered_at, expires_at?, complete, diagnostics[]
  tools[{original_name, safe_alias, schema_hash, input_schema,
         output_schema?, annotations, revision}]

McpInvocation
  operation_key, operation_id, execution_context
  server_id, original_name, schema_hash, arguments, deadline
  policy_decision_ref, remote_operation_ref?
```

公共引用字段的类型和语义见 01；profile 中只保存 secret ref，不保存明文凭据。
`protocol_policy` 是 Host 配置，不是自定义 MCP wire 字段；真正版本与能力由锁定 SDK 协商。
`connection_scope` 决定资源复用，不代表远端业务对象能够随连接自动恢复。

## 3. Gateway 内部接口

```text
McpGateway.inspect(profile_ref, access_context) -> ServerInspection
McpGateway.discover(profile_ref, access_context, cache_policy) -> CatalogSnapshot
McpGateway.diagnose(profile_ref, level, diagnostic_context) -> DiagnosticReport
McpGateway.invoke(McpInvocation) -> McpOutcome
McpGateway.cancel(operation_id) -> CancelOutcome
McpGateway.close_scope(scope_ref) -> CloseReport
McpGateway.read_resource(server_ref, uri, execution_context) -> ResourceContent  # P1
McpGateway.get_prompt(server_ref, name, arguments, execution_context) -> PromptContent  # P1
```

`McpOutcome` 保留文本、任意 JSON structured content、多模态块、resource links、`isError` 与服务来源。
结果分类为 success/tool_error/protocol_error/transport_error/cancelled/timeout/unknown_outcome，并映射到 07 的统一结果。
分类同时保留请求是否发送、远端操作引用与取消证据；HTTP 返回不等同于业务成功。
`CancelOutcome` 返回受理情况、传输是否关闭、可选远端确认和错误；`ServerInspection` 返回各诊断层状态与已观察版本/能力。
大型内容使用 01 的 ArtifactRef；不能把远端 URL 当作已经安全持久化的本地产物。

inspect/discover 接受 01 的 AccessContext；设置页 diagnose 使用 DiagnosticContext，可复用这两个入口但只做有界启动、认证、协商和目录/schema检查。它仍受 12 的端点、凭据、stdio启动白名单和资源策略约束，不能执行任意配置命令或 invoke 工具；诊断 client/进程结束时由受监督生命周期回收。示例工具调用需用户明确提交普通测试 run，经 07 授权及账本；无该证据时报告该层“未执行”。

## 4. 协议与连接实现步骤

1. 锁定 LangChain、FastMCP、SDK 的完整依赖集，在适配模块记录版本和已验证方法签名。
2. 每个 `(owner, workspace, server, credentials, isolation_scope)` 独立创建 client；凭据变化使该实例失效。
3. 混合 server 使用 `ClientGroup` 或独立 adapter 组合；不使用可能共享协商时代的 aggregate `MCPConfig`。
4. 实现 modern/legacy 两个明确兼容档；Host 不手工拼接初始化、请求元数据和重试 wire 消息。
5. stdio 使用受监督进程和结构化参数；stdout 只传协议，stderr 单独限量采集，退出交给 07 的进程管理能力。
6. HTTP 复用受控 client，并调用 12 的目的地/认证校验；将请求取消和客户端池销毁分开。
7. 记录真实协议版本、transport、能力和降级原因，形成连接诊断快照。
8. 释放作用域时关闭其连接资源；服务重启重新创建对象，远端业务句柄需单独核对。

modern 档按原调研的 2026-07-28 语义实现：请求自包含、HTTP POST、无协议 session；JSON 与请求级 SSE 均需支持。
该档不依赖 GET 消息流、`Mcp-Session-Id` 或 `Last-Event-ID`；Web 事件 SSE 的恢复由 10 独立负责。
legacy 档按协商版本维护 initialize/session/通知规则；旧 HTTP+SSE 仅在明确配置下启用，不宣称 WebSocket 支持。
`MCPAdapter` 返回的工具持有可重入 client；发现 context 退出后可重新连接调用，不应直接判为工具失效。
需要连续业务上下文时，受管理 context 覆盖 run 的适当作用域；协议无状态不免除身份和远端状态隔离。

## 5. 目录与执行集成步骤

1. 按 server、认证主体、scope、版本与配置 revision 分区缓存，遵守 private/TTL 等缓存提示。
2. cursor 当作不透明值：仅缺失/null 表示结束；空字符串仍可继续，重复 cursor 和总量超限必须有界终止。
3. 建立 `server_id/original_name/revision` 到安全别名的反向映射，按模型限制缩短并检查碰撞。
4. run 起点固定 schema hash；目录变化先使缓存失效，由安全边界刷新候选，不能静默换掉待执行参数约束。
5. modern 变更订阅按能力使用 `subscriptions/listen`，legacy 按其通知规则；未实现订阅时采用 TTL 与手动刷新。
6. 将工具适配结果注册到 07，保留来源、schema、风险提示；server annotations 不能直接授予权限。
7. invoke 只接受已由 Tool Gateway 形成的执行请求；复用其 OperationKey 和 ledger，不在本模块建立第二本账。
8. 分离 `isError` 业务失败、协议异常和 transport 异常；已发送的写操作失联交 07 核对结果。

网络重试、MRTR continuation 与业务重放关联同一逻辑 operation；新 JSON-RPC request id 不生成新的业务操作。
无公开幂等契约时不得因 429/503、超时或断流自动重复外部写入；具体准入由 07 的重试策略决定。
现代 HTTP 取消关闭对应请求响应流，stdio 按版本发送取消通知；取消不承诺撤销已经发生的副作用。
浏览器页面断开不得触发 MCP 取消，只有业务取消命令或执行 deadline 触发取消传播。

## 6. Elicitation 和未支持能力

在构建 P0 FastMCP Client 时必须显式注入 `elicitation_handler`，覆盖 adapter 默认 interrupt 行为。
收到请求时返回协议 `cancel`，记录 Host 原因 `unsupported_elicitation`，并限制请求/交互轮数。
该结果不是“用户主动取消”；不创建无人可答复的等待对象，不用伪造空表单继续工具写操作。
sampling、roots 和未知扩展返回明确 unsupported；能力广告必须与实际 handler 一致。
P1 将 handler 接入 Runtime 的持久交互机制，UI 表单归 11、HTTP 答复归 10、恢复调度归 06。
同一次 elicitation continuation 保持 OperationKey；用户表单确认不能替代 Host 权限批准或远端身份认证。

## 7. 诊断与 P1 增量

诊断逐层输出：进程/网络、认证、协议、发现、schema、已批准的只读示例调用；每层可独立为未执行。
“发现成功”只表示目录可读；不得标成所有工具端到端可用，诊断也不得任意执行未知工具。
P1 Resources/Prompts 直接使用 FastMCP client 相应能力，返回 Context Service 的来源内容，不假定 adapter 已封装全部 primitives。
P1 OAuth 的 token 存储与出站边界由 12 维护；本模块只持有服务绑定的凭据引用和认证状态。
远端长任务保留外部 task handle，不将其状态冒充本地 RunStatus；扩展逐项启用。

## 8. 交付物与验收证据

- 依赖锁文件、适配层契约样本、两代协议/两种主要 transport 的 profile 与诊断报告。
- client 隔离、重入调用、目录分页与 schema 快照记录；失败样本保留错误层级和发送状态。
- 对接 [P01](13-verification.md#p01)、[P02](13-verification.md#p02)、[P03](13-verification.md#p03)、[P04](13-verification.md#p04)，副作用恢复证据交 07 汇总。
- P1 提交额外能力的完整链路证据；不以 P0 tools/list 成功代替 OAuth、资源或人工交互验收。

## 来源与设计追溯

[原系统设计 §11 MCP](../通用Agent-Harness系统设计文档.md#s11)；[专项调研](../research/mcp-skills.md) §1–5、§8。
协议和库事实沿用 2026-09-17 调研基线；本指导未安装 SDK 或执行互操作测试，实施时按 13 保留实测结果。

<a id="mcp-continuation"></a>

## 9. P1 持久 MRTR continuation 契约（2026-09-17）

实现基线通过锁定 `mcp==2.2.0` 的官方 `Client` 包装，使用
`client.session.call_tool(..., allow_input_required=True)` 获取现代协议的 `InputRequiredResult`。
它与 legacy 推送式 elicitation 分开验收。SDK 和实测证据见 [兼容记录](../compatibility/mcp.md)。

`operation.metadata.protocol_continuation` 由本模块独占，包含
`kind=modern_mrtr, status=awaiting_input|sending, request_state_ref, request_hash, schema_hash,
interaction_id, round, server_id, config_revision`。
`request_state_ref` 指向平台凭据库，保存 SDK 返回的 opaque requestState 与 inputRequests，业务库只保存引用。
表单与 operation 由同一存储事务关联，且 request key 包含 operation_id、round、请求摘要。
当前版本每轮聚合本次服务返回的所有 form 请求；sampling、roots、URL-mode 和未知请求明确不支持。

恢复只把经过 interaction schema 验证的回答映射为 SDK `input_responses`，带回原始 `request_state`；
新 JSON-RPC request id 仍属于原 OperationKey。先 CAS 标记 sending，再执行且不自动网络重试。
服务器需要连续旧连接的 legacy elicitation 不具有跨进程续接保证；本实现对需要持久人工等待的
legacy profile 明确拒绝并解释能力限制，不以现代验收代替 legacy 恢复证据。
