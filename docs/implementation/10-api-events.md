# 10 · HTTP API 与事件流实现指导

[实现索引](README.md) · [公共契约](01-contracts.md) · 前置：[存储](02-storage.md)、[调度](06-scheduler.md)、[鉴权与配置](12-platform.md)

本文是 HTTP 路由、传输 DTO、状态码、事件 payload 和 SSE 恢复协议的唯一规范来源。
API 只协调请求，不执行长时间 Agent 循环；run 变更只调用 Scheduler，持久化只走 02 的 repository。
公共 EventEnvelope/ErrorEnvelope、标识与状态值直接导入 01，不在本文件另建副本。

## 1. 组件与分期

`apps/server/routes/` 按资源分路由；`schemas/` 定义输入输出；`services/` 做领域端口调用；`events/` 做 outbox 读取和 SSE 投递。
P0 包含工作区/会话/运行、交互答复、附件/产物、配置诊断及流式恢复；P1 开放分支、steering、记忆管理与 MCP 授权交互。
每次发布由 FastAPI OpenAPI 生成前端 client/types；手写前端类型不能成为另一套 API 契约。
认证、Cookie、Origin、CSRF 和敏感输出处理统一使用 12 的中间件，不由各路由复制实现。

## 2. HTTP 路由清单

资源 ID 一律使用 01 标识；分页集合接受不透明 `cursor` 和有上限的 `limit`，返回 `items/next_cursor`。
GET 默认 `200`；下表单独标出创建/异步状态。带 P1 的路由在 P0 返回 `501 FEATURE_NOT_ENABLED`。

| 方法与路径 | 请求要点 → 响应要点 / 服务所有者 |
|---|---|
| `POST /api/auth/exchange` | 一次性启动 ticket → `204`、会话 Cookie；12 |
| `POST /api/auth/logout` | 当前会话 → `204`；12 |
| `GET /api/workspaces` | → 工作区摘要集合；02 查询 |
| `POST /api/workspaces` | `name, root_candidate` → `201` 工作区；12 验证、02 保存 |
| `GET /api/sessions` | `workspace_id` → 会话摘要集合；02 查询 |
| `POST /api/sessions` | `workspace_id, agent_spec_id, title?` → `201` 会话和默认 branch；02 |
| `GET /api/sessions/{id}` | → 会话、branch、run 摘要及已提交历史；02 查询 |
| `PATCH /api/sessions/{id}` | `title?, archived?, expected_revision` → 当前会话摘要；02 |
| `POST /api/sessions/{id}/runs` | SubmitRunInput → `202` SubmitReceipt；06.submit |
| `GET /api/runs/{id}` | `view=state|snapshot` → RunView；02 一致性查询 |
| `GET /api/runs/{id}/events` | `after_seq?` 或 Last-Event-ID → SSE；本模块 |
| `POST /api/runs/{id}/cancel` | `reason?` → `202` CancelReceipt；06.cancel |
| `GET /api/operations/{id}` | → 工具执行、未知结果原因与可见核对记录；07/02 |
| `POST /api/operations/{id}/reconcile` | 07 的 ReconciliationInput → `202` 核对回执与实际状态；06.reconcile |
| `GET /api/interactions/{id}` | → 脱敏交互详情、审阅材料引用及当前 revision；05/02 |
| `POST /api/interactions/{id}/respond` | InteractionResponse → `202` ResponseReceipt；06.respond |
| `GET /api/runs/{id}/context` | → 预算、来源、摘要与归档引用；04 |
| `POST /api/runs/{id}/context/pins` | `text, expected_context_revision` → branch 固定内容集合及新 revision；04，复用本章幂等要求 |
| `POST /api/artifacts` | multipart `workspace_id, file` → `201` ArtifactRef；02 ArtifactStore、12 授权 |
| `GET /api/artifacts/{id}` | `disposition=attachment|preview` → 受控字节流；02/12 |
| `GET /api/agents` | → AgentSpec 可见摘要和 revision；05 |
| `GET /api/config/{kind}` | → 可见配置摘要；12 |
| `GET /api/config/{kind}/{id}` | → 脱敏配置及 revision；12 |
| `PUT /api/config/{kind}/{id}` | 配置正文、`expected_revision` → 保存后配置；12 |
| `POST /api/models/{id}/test` | 固定测试项选择 → 诊断结果；03，禁止任意提示词代理 |
| `GET /api/model-providers` | → 提供方预设、是否需要 Key、可编辑地址、模型选项及官方来源；03 |
| `POST /api/model-setup/discover` | `provider_id, api_key?, profile_id?, base_url?` → `items[{id,name}], source, message?`；03，有界目录发现 |
| `POST /api/model-setup/test` | 上述字段及 `model_id` → `connected, tool_calling, message`；03，临时固定探针 |
| `POST /api/model-setup/save` | 上述字段及 `expected_revision?` → `profile, id, revision, active, message`；03，验证后保存并用于默认 Agent |
| `GET /api/skills`、`GET /api/skills/{id}` | → 08 的目录/详情，详情可含正文快照 |
| `POST /api/skills/refresh` | `source_ids[]` → RefreshReport；08 |
| `PATCH /api/skills/{id}` | `enabled, expected_revision` → 当前目录记录；08 |
| `GET /api/plugins` | → 内置插件版本、注册状态与诊断；08 |
| `POST /api/plugins` | P1：`manifest: PluginManifest` → 已校验的静态声明注册回执；08 |
| `POST /api/plugins/{id}/versions/{version}/drain` | P1：停止版本接受新绑定，已有固定任务保留可用引用 → 排空状态；08 |
| `GET /api/mcp/{id}/catalog` | → 09 的目录和完整性标记 |
| `POST /api/mcp/{id}/diagnose` | 允许的诊断层级 → 分层报告；09 |
| `POST /api/sessions/{id}/branches` | P1：`source_checkpoint_ref, side_effect_policy, expected_branch_revision` → `201` branch；06.create_branch |
| `POST /api/runs/{id}/input-commands` | P1：`text, expected_input_revision` → `202` 命令回执；06.steer |
| `POST /api/mcp/{id}/authorize` | P1：授权开始 → 浏览器授权目标与交易 ID；09/12 |
| `GET /api/mcp/oauth/callback` | P1：协议回调参数 → 受控本地结果页；09/12 |
| `GET /api/memories` | P1：`workspace_id, scope?, review_state?, cursor?` → MemoryRecord 摘要集合；04.MemoryService.list |
| `GET /api/memories/{id}` | P1：→ MemoryRecord 详情、来源与 revision；04.MemoryService.get |
| `POST /api/memories/{id}/review` | P1：`decision, expected_revision` → 审阅后记录；04.MemoryService.review |
| `PATCH /api/memories/{id}` | P1：可编辑字段、`expected_revision` → 新 revision 记录；04.MemoryService.update |
| `DELETE /api/memories/{id}` | P1：`If-Match: <revision>` → `204`；04.MemoryService.delete |
| `GET /health/live`、`GET /health/ready` | → 最小健康状态；12，无内部日志和密钥 |

`kind` 只允许 `models/agents/mcp/skill_sources/policies`，每类正文引用其所有者 schema；禁止动态导入类名或任意文件写入。
配置 revision 使用乐观并发；新建 PUT 要求显式不存在条件，更新要求已读取 revision，成功返回 `201` 或 `200`。
有界诊断可同步返回；超过诊断 deadline 返回可解释错误，不占用 Agent worker，不能无限等待。
诊断路由由 Host 创建 01 DiagnosticContext，再调用 03.verify 或 09.diagnose；不接受客户端提交内部身份。响应记录固定测试项和真实执行层级；需要工具示例测试时使用正常run提交入口，不把诊断按钮变成旁路工具执行入口。
会话归档只更新可见性和 revision，已有 run 继续按 Scheduler 生命周期运行；列表可使用 `archived` 过滤查看归档会话。
上传前验证 owner 对工作区的授权，按 12 配置检查 MIME/配额/最大字节；读取流时继续计数，超限返回 `413`。
文件完整落盘并登记后才返回 ArtifactRef；原文件名仅作展示，不作为宿主目标路径，后续提交再次校验引用归属。
上传幂等正文 hash 基于实际文件 hash 与规范化元信息，不包括 multipart boundary；中断上传不产生可提交附件引用。
尚未关联 run 的上传只返回 HTTP 回执，不伪造缺少 run_id 的事件；run 引用附件后使用对应运行的事件流。
记忆审阅、更新和删除语义归 04；API 不自行改变 scope、可信状态或遗忘策略，服务端按当前身份过滤结果。

## 3. 提交、答复与幂等输入

模型简化配置的 `api_key` 是仅入站的 SecretStr，不得复用普通配置 DTO、持久命令正文或浏览器重试缓存。discover/test 不登记持久业务命令；save 必须携带 Idempotency-Key，持久指纹只含去除 Key 的正文和使用当前配对会话令牌计算的 Key HMAC。同一会话的同键同正文重放原回执，正文不同返回冲突；跨重新配对不重放敏感请求。维护窗口拒绝模型发现、测试与保存，所有入口继续应用 Cookie、Host、Origin、CSRF。原始密钥、供应商鉴权响应不得出现在验证错误和通用异常中。

`SubmitRunInput = {branch_id, expected_branch_revision, agent_spec_revision, content_parts[], attachment_refs[], selected_skill_refs[]}`。
`SubmitReceipt = {run_id, branch_id, branch_revision, status, revision}`，返回前由 Scheduler 完成入队事务。
用户追加普通输入仍提交新 run 并排队；P1 input-command 才写入既有 run，不能一份输入同时进入两条通路。
`InteractionResponse = {expected_revision, response, binding_ref?}`；response 按对应交互 schema 校验，批准必须绑定当前 ApprovalBinding。
交互详情为 05 InteractionRecord 的脱敏投影；只有 open 记录提供答复入口，pending_binding 不能提前暴露为可批准操作。
`CancelReceipt/ResponseReceipt` 返回目标 ID、实际 status、revision 和是否已受理；受理取消不声称进程已退出。
`RunView(state)` 返回状态、revision、结果摘要、错误、活动交互引用和最近耐久 seq。
`RunView(snapshot)` 另含已提交消息、工具摘要、开放交互、子任务/产物引用及 `last_seq`；同一一致性读取边界内获取。
snapshot 内容必须与 last_seq 截止点一致；不能混入之后读取的“最新状态”或独立 checkpoint 库的未协调进度。
核对回执只表示决定已受理；实际动作由 07 核验，06 按结果推进。仅显示给用户当前权限允许的核对选项，接口不得提供直接指定 RunStatus 的字段。

`MessageView = {message_id, role, content_parts[], model_attempt_id?, usage_ref?}` 是本模块唯一公开消息投影，role 为 `user|assistant|tool`。
P0 content part 为 `{type: text, text}` 或 `{type: file_reference, artifact_ref, label?}`；artifact_ref 使用 01 的 ArtifactRef。
验证 profile 支持图片后才开放 `{type: image, artifact_ref, alt?}`；未知能力返回可解释校验错误，不静默丢弃图片。
提交输入和已提交事件复用此 content part schema；附件引用按 artifact_id 去重，Context Service 只解析一次。
provider opaque metadata、签名续接字段、凭据及内部提示不下发 UI；03 的完整模型消息与公开 MessageView 分别保存/转换。

所有业务变更请求携带 `Idempotency-Key`；认证兑换/OAuth 回调使用各自一次性协议凭证。
幂等键作用域为已认证 owner、方法和规范化路由目标；记录规范化正文 hash 与最终响应引用。
同键同正文返回已存回执；同键不同正文返回 `409 IDEMPOTENCY_CONFLICT`；并发首发只允许一个成功创建。
幂等命中必须先于业务 revision 重验，避免已成功提交的网络重试因旧 revision 被拒绝；仍须当前访问检查。
执行中命中可返回 `202` 和原操作查询目标；保留窗口在部署配置公布，客户端不得无期限假设键仍有效。
API 幂等键不替代 OperationKey，后者只由 07 管理工具副作用。

## 4. 状态码与错误

`400` 为互斥参数/请求语义错误，`401` 未认证，`403` 明确无权限；需隐藏资源存在性时统一 `404`。
`409` 用于 revision、幂等冲突、交互已处理或事件游标过期；`410` 为已过期交互/已按保留策略删除的产物。
`413` 为输入超限，`422` 为 schema/ID/游标格式错误，`429` 为限流，`503` 为服务未就绪。
错误正文使用 01 ErrorEnvelope；`code` 可稳定供客户端分支判断，`message` 用于用户展示。
禁止把内部异常栈作为错误正文；大诊断保存在受权限控制的 `details_ref`。

## 5. 事件 payload 注册表

各行只定义 EventEnvelope 的 `data`；字段类型引用所有者，`?` 为可选。关键状态与已提交内容为 durable。

| type | data 必需字段及可选字段 |
|---|---|
| `run.queued/started/state_changed/completed/failed/cancelled` | `revision, status, reason?, result_ref?, error?` |
| `message.delta` | `message_id, stream_id, chunk_index, text`；唯一 ephemeral 类型，chunk_index 从 0 单调递增 |
| `message.committed` | 完整公开 MessageView；内容必须已经持久化 |
| `tool.prepared/started/completed/failed` | `operation_id, tool_id, tool_revision, input_summary, result_status?, result_summary?, artifact_refs[], error?` |
| `interaction.required` | `interaction_id, revision, kind, prompt, response_schema, binding_ref?, expires_at?` |
| `interaction.resolved` | `interaction_id, revision, resolution, response_summary?` |
| `context.compacted` | `summary_ref, source_message_range, before_tokens, after_tokens, estimate_method` |
| `child.created/completed` | `child_run_id, goal_summary, status, result_ref?`；P1 |
| `artifact.created` | `artifact_ref, display_name, operation_id?` |
| `usage.updated` | `revision, usage_summary, cost_estimate?, estimate_basis?, missing_fields[]` |

`role/content_parts` 使用本文件 MessageView；`kind/response_schema` 引用 05 的 InteractionRecord；结果摘要由 07 脱敏投影。
`result_status` 引用 07 ToolResult；tool.completed 表示结果已登记，UI 仍按 result_status 区分成功、取消或未知。
`resolution` 是交互结果摘要，不作为权限凭证；run/child status 引用 01，交互 status 引用 05；usage 缺失不能传为零。
工具完整参数、秘密和未过滤原始内容不进入事件；审批展示所需材料通过受控交互查询投影提供。
最终消息先持久化，再产生 message.committed；run 完成事件不能早于其已提交输出和关键工具结果。

## 6. SSE 帧、恢复与背压

durable 帧用 `id: <run_id>:<seq>`、`event: <type>`，`data` 为完整 EventEnvelope JSON；seq 由 02 原子分配。
ephemeral 帧不写 `id`，EventEnvelope.seq 为 null；不能改变客户端最后耐久游标。
心跳使用 SSE 注释行；它不是事件，不写数据库，也不推进 seq。
首次订阅从 `after_seq=0` 开始；重连使用 Last-Event-ID，若同时提供 after_seq 则两者必须相同。
游标中 run_id 与 URL 不一致返回 `422`，未来 seq 返回 `409 INVALID_CURSOR`，不能默默跳过缺失事件。
打开流前查询最小保留 seq；过期返回 `409 CURSOR_EXPIRED`，客户端读取 snapshot 后从其 last_seq 重新订阅。
服务端使用“订阅唤醒 + 数据库按 seq 补读”，接入时再次补读；不依赖瞬时内存消息覆盖查询与订阅之间的间隙。
投递按 seq 排序，允许重复；客户端仅在 reducer 已接纳后更新耐久游标，重放不重新执行任何命令。
临时文本按 stream_id/chunk_index 去重；重连不补临时片段，完整 committed 消息替换该 message_id 的临时展示。
崩溃留下的未提交片段标记为中断；snapshot 不得把片段当作完整历史，新的模型尝试使用新 stream_id。
每个订阅设置队列和字节上限；临时 delta 可合并，耐久事件不能丢弃，慢客户端断流后自行恢复。
outbox 发布器和 API 进程可重启，消息事实仍由数据库决定；HTTP/SSE 连接的关闭不取消 run。

## 7. 开发顺序与交付证据

先生成 schema 和错误映射，再接 Scheduler 提交/查询；随后加入持久事件、snapshot 恢复、临时 delta，最后接配置管理。
路由单元只调用已注入端口；不直接写 SQL、改 RunStatus、执行工具或拼接 graph resume。
交付 OpenAPI、生成 client、事件序列样本、游标恢复日志和关键错误响应样本。
验收定义集中在 [13 的 U01](13-verification.md#u01)、[R04](13-verification.md#r04)、[CT01](13-verification.md#ct01)，这里只提交对应传输证据。

## 来源与设计追溯

[原系统设计 §14 API 与 Web](../通用Agent-Harness系统设计文档.md#s14)、[§13 数据一致性](../通用Agent-Harness系统设计文档.md#s13)。
[MCP 专项调研](../research/mcp-skills.md) §5 提供 Web 断线与 MCP 取消的边界；本文新增路由细节是实现决策，不声称为框架原生 API。
