# Mi-Harness 使用反馈改进指导

> 分析日期：2026-09-18。
> 分析基线：`main`，提交 `21b61c605e4d142b6d61551c119517b4a819fa8a`。
> 本文是基于源码和官方资料的实施方案，不是功能已经实现的声明。本次交付只新增指导文档；未运行项目测试、真实供应商联调或性能压测。下文新增的接口、组件、记录和指标均为建议，现有实现会明确标注。

## 0. 结论与实施边界

建议把产品收敛为：**一个对外 Agent、两种执行模式、会话独立选择模型、运行中按模型调用边界切换、有序图文消息、文件驱动的 MCP 配置、可复用的模型验证结果。**

不应重写 Agent 循环。继续由 LangChain `create_agent` 驱动模型/工具循环，由现有 worker、LangGraph checkpoint、ToolGateway、权限策略和操作账本保障执行与恢复。主要工作是补齐已有能力的接线、持久化控制和用户交互，而不是替换底层框架。

| 反馈 | 源码现状 | 推荐改进 |
| --- | --- | --- |
| 1. 会话 Agent 选择改为模式选择 | 工作台维护 `agentId`、`newSessionAgent`，创建会话时提交 `agent_spec_id` | 改为独立的 `react` / `plan` 模式选择；对外固定一个 Agent |
| 2. 会话选择模型，循环中下一次调用切换 | 创建 graph 时固定模型；网关已有 `switch()`，但会话没有控制入口 | 增加会话偏好、持久化运行控制指令，在下一次逻辑模型调用边界接入已有切换方法 |
| 3. 不允许编辑 Agent | 设置页和通用配置 API 均允许编辑 Agent | 移除公开编辑入口并在后端拒绝写入；保留内部 AgentSpec、历史快照及必要的委派机制 |
| 4. 上传图片并保留插入位置 | API 已有 `ImagePart`，上下文层能物化图片；输入框仍是文字与附件分离，消息组件只取文字 | 以有序 `content_parts` 为唯一消息结构，补齐编辑、上传、渲染、视觉验证和图片预算 |
| 5. MCP 直接编辑 JSON 文件 | 设置页编辑数据库记录，已有 JSON 草稿，但不是磁盘文件 | 新建受控 `mcp.json` 文件作为用户配置源，Web 展示其原文，数据库保存生效投影和历史版本 |
| 6. 模型验证与保存慢 | 测试、保存分别执行完整探测；单次最多四个请求，探测总超时 45 秒 | 分离保存与验证、复用验证凭证、移除保存关键路径的可选探测、展示真实阶段和耗时 |

### 必须保留的约束

- 不修改已经产生的运行配置快照，不清空对话、checkpoint 或工具账本来实现切换。
- API Key 继续保存在系统凭据库；模型与 MCP 配置仅引用凭据，不把明文秘密写入 JSON、日志、通用命令记录或前端持久化存储。
- 模型能力必须有依据。不能因为模型名称看起来支持视觉，就标记 `vision=verified`；不能为提速跳过工具协议验证却直接激活模型。
- 模式、模型、工具权限是不同维度。切换模型不扩大权限，Plan 模式不能仅依赖提示词保证只读。
- 本文参考 Codex / CodeBuddy 的交互方式，但“运行中下一次调用生效”是 Mi-Harness 需要明确实现和测试的契约，不能据此声称竞品已提供完全相同的语义。

## 1. 源码依据与修改入口

下表中的路径、类和函数均已在分析基线中核对。相对链接指向仓库文件；实施时以基线提交与待开发分支的差异为准，不机械依赖旧行号。

| 现有文件 | 已确认的行为 | 对本次改造的影响 |
| --- | --- | --- |
| [App.tsx](apps/web/src/features/workbench/App.tsx) | `loadCatalogs()` 拉取 projects、agents、skills；`send()` 只将文字放入 `content_parts`，另传 `attachment_refs`；`MessageContent()` 只提取文字 | 需要拆分会话控制组件、改造有序草稿和消息渲染 |
| [Settings.tsx](apps/web/src/features/settings/Settings.tsx) | `agents` 为可编辑配置；MCP 使用通用配置编辑，维护 `draft` / `jsonDraft` | Agent 去编辑必须同时改 UI 与 API；MCP 不能仅把现有 textarea 放大 |
| [schemas.py](harness/server/schemas.py) | 已有 `TextPart` / `FilePart` / `ImagePart`；`SubmitRunInput` 无模式和模型选择；`SteeringInput` 仅接收文字 | 复用内容类型，新增独立的模型控制契约，不把控制伪装成聊天文字 |
| [runtime/models.py](harness/runtime/models.py) | `AgentSpec` 为不可变配置，包含模型策略、工具、MCP、预算等，没有模式字段 | 保留内部规格，新增受控模式策略 |
| [runtime/engine.py](harness/runtime/engine.py) | `_build()` 解析模型并创建 graph；`awrap_model_call()` 按原 spec 选择上下文配置，复制原 handle；`resume()` 使用原始快照 | 运行中的模型不会因为设置页更新而自动变化；切换应发生在 middleware / gateway 边界 |
| [model_gateway/gateway.py](harness/model_gateway/gateway.py) | 已有 `switch()`：检查切换、重建上下文、解析目标 handle；`resolve()` 缓存底层客户端；`_call()` 管理重试、预算和模型事件 | 复用已有切换方法和客户端池，不新增第二套模型请求通路 |
| [model_gateway/protocol.py](harness/model_gateway/protocol.py) | `check_model_switch()` 校验工具调用配对，并拒绝明确不可移植的 continuation | 热切换不能直接把所有供应商私有字段传给另一供应商；当前检查还需要补齐能力与完整历史兼容性 |
| [context/service.py](harness/context/service.py) | `_attachments()` 已能按顺序将授权图片引用变成标准图片块；校验视觉能力和单图 10 MiB；`_summary_profile()` 仍读取固定配置 | 图文链路已有基础，但模型切换必须同时协调摘要模型和上下文预算 |
| [server/composition.py](harness/server/composition.py) | `make_snapshot()` 从 Agent 冻结模型、工具和 MCP；通用 `save_config()` 允许写 Agent / MCP | 新提交选择必须进入快照；取消 Agent 编辑后要重新明确工具与 MCP 的配置入口 |
| [scheduler/service.py](harness/scheduler/service.py) | `submit()` 分别保存 `content_parts` / `attachment_refs`；附件校验和 run 引用登记主要遍历后者 | 必须统一收集内联内容中的引用，防止漏校验、漏登记和重复插图 |
| [scheduler/worker.py](harness/scheduler/worker.py) | 独立进程读取数据库；构造 HumanMessage 时保留 `content_parts`；`message_commit()` 未传递完整模型归属信息 | 内存变量不能作为模型切换事实源；补齐持久化控制与消息归属 |
| [server/app.py](harness/server/app.py) | 已有上传接口、`disposition=preview` 附件预览和模型设置路由；保存模型对秘密做指纹而非明文持久化 | 复用认证、预览、幂等和秘密处理机制，不另建不受保护的图片下载服务 |
| [server/model_setup.py](harness/server/model_setup.py) | `test()`、`save()` 都调用 `_probe()`；保存后更新默认 Agent 的模型策略；未授予视觉能力 | 重复验证是明确的结构性耗时；默认模型与会话选择应解耦 |
| [model_gateway/profiles.py](harness/model_gateway/profiles.py) | `ModelProfile` 固定版本；`supports()` 只接受带证据的 verified 能力 | 下拉选项使用 `profile_id@revision`，不可只存供应商 model_id |
| [mcp/models.py](harness/mcp/models.py) | `ConnectionProfile` 已有传输方式、版本、超时及凭据引用约束 | 文件格式应编译到现有模型，不绕过校验 |

## 2. 统一交互与状态语义

### 2.1 会话输入区

输入区保留常驻、相互独立的两个入口：

```text
[ ReAct ▾ ]  [ 提供方 / 模型名称 ▾ ]       [添加图片/文件]

比较下面两张图：
[图片 A 缩略图]
重点关注这个变化：
[图片 B 缩略图]

                                  [发送 / 停止]
```

模型下拉采用可搜索列表，显示提供方、模型名、工具能力、图片能力、配置状态和选中标记。不可用项目可显示为禁用项并解释原因，例如“工具能力待验证”“当前对话包含图片”“受启动配置固定”。不要每次展开列表都访问供应商发现接口；会话选择的是本机已经配置的模型。供应商发现仍属于添加模型流程。

运行中，模型选择器保持可操作；旁边区分：

- 当前调用：模型 A。
- 已选择模型 B，等待下一次调用生效。
- 收到后端生效事件后：当前模型 B。

失败时显示错误并保留真实状态，不能仅修改前端标签制造已经切换的假象。刷新、SSE 断线重连和另一浏览器标签页打开，都应能从后端恢复同样的状态。

### 2.2 作用范围

**会话偏好**决定后续提交的默认模式和模型；**运行控制**只改变指定活动 run 的后续模型调用。不要更新全局默认 Agent 的模型来模拟会话选择。

推荐优先级：启动配置的明确强制固定项 > 本次提交明确选择 > 当前会话偏好 > 应用默认值。当前代码中的 `settings.model_profile_ref` 具有固定含义，不能被下拉框静默覆盖；受固定时禁用相应操作并说明原因。

会话选择模型时，如存在用户当前操作分支的活动 run，可用一个后端事务同时更新会话偏好并创建运行控制指令；若用户只是查看历史 run，不允许误改历史 run。存在多分支时，界面必须明确目标活动分支；不要修改其他分支已经排队或执行中的 run。排队 run 已有初始快照，只有显式针对该 run 的控制指令才能改变它的选择。

### 2.3 模式切换的生效规则

反馈明确要求模型可以在循环中热切换，但没有要求模式也必须热切换。第一版建议：**模式对单个 run 固定；运行中更改模式只影响下一次用户提交。** 界面同时标明“当前运行：ReAct / 下次提交：Plan”，不能把仍在写文件的运行显示成 Plan。

这样避免 Plan → ReAct 在已有审批与工具执行期间隐式扩大权限。需要立即更改当前模式时，先停止或完成当前 run，再以目标模式发起新 run；模型热切换不受此限制。

## 3. 一个 Agent、两种模式（反馈 1、3）

### 3.1 产品与配置边界

新增受控 `AgentMode = Literal["react", "plan"]`，对外 Agent 名称固定为 Mi-Harness。移除工作台 `agents` / `agentId` / `newSessionAgent` 的用户选择逻辑，创建新会话由服务器绑定内部默认 Agent。

设置中的 Agent 模块替换为只读“助手与模式”说明，或移除该模块并将模式说明放在选择器旁。不得再暴露自定义 system prompt、Agent ID、模型引用、工具数组等 Agent 编辑字段。

同时修改公开接口：`/api/agents` 只返回单一公开助手描述；通用 `PUT /api/config/agents/{id}` 返回稳定的 `AGENT_READONLY` 错误；公开 Agent 配置读取不再枚举内部模板。旧版客户端提交非公开 `agent_spec_id` 时拒绝或进入明确的兼容转换，不可继续创建自定义公开 Agent。插件贡献也不能重新暴露多个用户可选 Agent。

内部仍保留 `AgentSpec`、委派、预算及历史配置读取。一个对外 Agent 不等于删除内部子任务机制。新增 run 的快照可包含 `mode` 与 `mode_policy_version`，而不是创建两个用户可编辑的 Agent 配置。

### 3.2 模式行为

| 项目 | ReAct | Plan |
| --- | --- | --- |
| 目的 | 在授权范围内分析、调用工具并完成任务 | 澄清需求、读取证据、形成可审阅计划 |
| 模型与工具循环 | 现有 `create_agent` | 同一 `create_agent`，不同受控策略与工具集合 |
| 项目文件写入 | 按现有权限与具体审批 | 禁止 |
| 通用命令执行 | 按现有权限与具体审批 | 默认禁止，不能靠命令名猜测是否只读 |
| MCP | 仅允许已配置、已授权且在运行快照中的能力 | 仅宿主已认可的只读工具；未知副作用默认禁止 |
| 计划状态 | 可引用经确认计划执行 | 可写入宿主管理的计划记录，但不得借此任意写项目文件 |
| 委派 | 保留现有预算与权限交集 | 子任务必须继承 Plan 的权限上界，不能借委派越权 |

Plan 提示词应要求输出目标、现状依据、实施步骤、涉及文件、风险、验证方式和待确认问题。通过现有计划存储能力形成可追踪计划；明确区分“保存宿主计划记录”和“修改用户项目文件”。

“按此计划执行”应绑定 `plan_id`、计划版本/内容 hash 和用户确认，创建一个 ReAct run，并延续当前分支上下文；计划确认不等于提前批准所有后续副作用。计划发生变化后，旧确认不能继续授权新计划。

### 3.3 后端双重约束

`make_snapshot()` / worker `tools_factory()` 必须对 Plan 提供的工具目录进行过滤。当前 worker 会从冻结的 `snapshot.tools` 构建工具，**只改 `AgentSpec.tools` 不足以保证过滤正确**。

ToolGateway / PolicyEngine 在实际执行时再检查模式权限；即使模型返回了未展示的危险工具、MCP 伪报只读、Skill 提示要求执行命令，也必须拒绝。MCP 的 `readOnlyHint` 等 annotations 只能是参考，不能独立构成授权依据。[R5]

Agent 编辑移除后，模型默认值归模型设置管理，MCP 启用归 MCP 设置管理，权限上界归权限设置管理，技能选择仍在现有入口。宿主据此组装默认 Agent 的受控快照；不能让用户为了启用 MCP 再去编辑隐藏的 `agent.mcp_servers`。

## 4. 会话模型选择与运行中热切换（反馈 2）

### 4.1 现有能力与缺口

当前 `_build()` 已经把模型 handle 传入 graph，`awrap_model_call()` 又按原始 spec 构建上下文，因此修改默认配置不会影响正在运行的模型。另一方面，`ModelGateway.switch()` 已经存在，并调用 `check_model_switch()`、重建上下文、解析目标模型。推荐复用该入口，而不是新增另一套供应商请求代码。

LangChain 官方提供在模型调用 middleware 中通过 `request.override(model=...)` 动态选择模型的方式，可作为接线依据；具体签名与行为必须以项目锁定版本验证，不能为使用新示例而直接全量升级依赖。[R1]

### 4.2 新增持久化记录

可先使用 Store 现有命名空间/事务/CAS 能力，避免无必要地大改数据库；若使用专用表，应通过明确版本迁移引入。

| 建议记录 | 关键内容 |
| --- | --- |
| `session_preferences` | session_id、revision、mode、model_profile_ref |
| `run_model_controls` | run_id、control_revision、desired_profile_ref、effective_profile_ref、effective_revision、最新指令状态 |
| `run_model_commands` | command_id、run_id、目标 profile_ref、提交者、接受时刻、状态、拒绝原因、被覆盖指令 |
| `model_call_bindings` | logical_call_id、run_id、purpose、checkpoint/node 标识、control_revision、profile_ref、input_view_id、worker fencing token |

运行初始快照保持不可变。后续选项变化是独立、可审计的控制记录，而不是覆写 `snapshots`、`runtime_snapshots` 或原 `AgentSpec`。模型版本必须固定为 `profile_id@revision`；编辑相同模型配置产生的新 revision 不得自动替换已有 run 的目标版本。

控制修订号与 `input_revision`、分支修订号分开：模型选择不是用户新消息，不应冒用文字 steering，也不应使已经绑定的工具审批无故失效。

### 4.3 建议 API 契约

以下均为新增或扩展契约，不代表现有路由已实现。

| 接口 | 职责 |
| --- | --- |
| `GET /api/agent-modes` | 返回受控模式、说明、默认模式和模式策略版本 |
| `GET /api/models/available?session_id=...` | 返回本机已配置模型的可选投影、固定版本、能力和禁用原因；不触发供应商推理 |
| `PATCH /api/sessions/{id}/preferences` | 带 `expected_revision` 保存未来提交偏好，不影响已有 run |
| 扩展现有 run 提交接口 | 接受 `mode`、`model_profile_ref`，服务器在提交时校验并写入初始快照 |
| `POST /api/runs/{id}/model-selection` | 带幂等键与 `expected_control_revision` 创建下一调用生效的控制指令 |
| 扩展 run 查询与事件 | 返回 desired/effective model 和 revision，发布 requested/applied/rejected/superseded 状态 |

运行切换请求示例：

```json
{
  "model_profile_ref": "model-secondary@3",
  "expected_control_revision": 7,
  "persist_for_session": true,
  "expected_preferences_revision": 4
}
```

`persist_for_session=true` 时必须校验会话归属、活动分支和偏好版本，在同一个 app.db 事务内保存偏好与控制指令；任一冲突则整体失败。返回 202 表示接受，不表示已经使用该模型完成推理。首次 `control_revision` 的初值须在服务端与 OpenAPI 中统一。

所有写接口复用现有认证、Origin/CSRF、幂等处理和权限校验。不可用模型返回明确的错误；终态 run 返回 409；并发版本冲突保留用户选择草稿，由前端读取最新状态再处理，不能盲目重发覆盖其他窗口的选择。

### 4.4 “下一次调用”的精确定义

本方案将其定义为：**指定 run 的主 Agent 循环中，下一次尚未取得持久化派发绑定的逻辑模型调用**。当前正在输出的响应和同一次调用的供应商重试继续使用原模型；重试不是新的 Agent 推理步骤。

| 用户切换时机 | 行为 |
| --- | --- |
| 模型 A 正在生成 | 不打断 A；下一次循环调用使用 B |
| 工具执行中、工具结果尚未齐全 | 接受指令；等所有必要工具结果就绪后，在下一次模型边界切换 |
| 等待用户审批或子任务 | 接受指令但不跳过等待；恢复后的下一次调用切换 |
| run 尚在队列 | 接受针对该 run 的指令；首次实际调用使用选择后的模型 |
| 快速选择 B 后又选 C，期间没有派发调用 | 最后接受的 C 生效，B 标记 superseded；保留两次审计记录 |
| 已派发 B 调用后选择 C | B 调用完成；下次使用 C |
| 当前响应直接结束 run | 不凭空增加模型调用；未生效指令标记未应用，已保存的会话偏好留给下次提交 |
| run 已结束或正在取消 | 不再接受热切换；提示仅能修改后续会话偏好 |

特别注意：`check_model_switch()` 会拒绝未配对的工具历史。因此**不能在工具执行中接收 REST 请求时就对那份未完成历史调用它并直接拒绝切换**。REST 阶段检查配置、授权和静态能力；完整协议历史检查放在工具结果已提交的下一模型边界。

### 4.5 运行时接线与恢复

建议新增 `harness/runtime/model_selection.py` 封装控制读取、版本比较及调用绑定，避免把整套业务规则塞入已有大文件。

调用顺序：

1. 模型 middleware 检查取消、期限、worker fencing token，读取最新目标 profile 和控制 revision。
2. 使用实际完整消息历史做切换预检。通过现有 `ModelGateway.switch()` 获取目标 handle，并按目标限制重建 ContextView；即使目标上下文窗口更大，也重新计算工具 schema、输出预留和图片预算。
3. 在派发前以短数据库事务再次比较控制 revision，持久化 `model_call_bindings`。若准备上下文期间收到新选择，丢弃尚未派发的旧候选，按最新选择重新准备；重复准备不等于增加一个外层 Agent 循环。限制高频切换并合并未派发请求，防止饥饿。
4. 将目标 **GatewayModelHandle** 传给 `request.override(model=...)`，由现有工具绑定流程绑定当前工具集。不能只更新 profile 标签，也不能把旧模型预绑定的 runnable 当成新模型使用。
5. 派发后记录 applied 事件和实际 profile。该逻辑调用的所有重试固定使用同一绑定，保持流式内容、费用与归属一致。
6. assistant 消息提交时关联 logical_call_id、model_attempt_id、profile_ref、control_revision；run 投影、SSE reducer 和历史气泡均显示真实使用的模型，不从当前下拉值倒推历史。

持久化派发绑定是并发语义的线性化点，不是对“网络字节已经发出”的证明。外部 HTTP 与本地数据库不能构成同一个原子事务；请求已发送但响应未知的情况，继续走网关现有失败与预算核对规则，不宣称完全 exactly-once。

当前 app.db 与 checkpoints.db 分离，不能依赖它们同一事务提交。logical_call_id 应由稳定的 run/checkpoint/node/输入版本信息形成，恢复时优先复用该调用已经写入的绑定；已接受但尚未应用的更高 revision 留给下一调用。不得因为从旧 checkpoint 恢复而把整个 run 的模型控制回退。所有写入校验当前 claim 的 fencing token，旧 worker 不得覆盖新 worker 的状态。

需要覆盖四个故障点：指令接受后、上下文准备后、调用绑定后但网络请求前、响应收到后但 graph checkpoint 前。工具副作用仍由原 OperationKey 和账本防重，不因模型切换重新执行。

### 4.6 能力、历史与辅助调用

- 目标模型必须经过当前 Agent 所需的文本与工具协议验证，并通过目标 endpoint / credential / 出站策略校验。`switch()` 本身不能替代全部产品层校验。
- 有效上下文中包含图片时，目标必须具备已验证视觉能力。不能只检查当前输入框而忽略历史图片；不能静默删除图片再继续回答。
- 工具调用 ID 与结果必须保持完整配对。供应商私有 continuation、签名或 opaque reasoning 不能盲目跨供应商传递。现有 `portable=false` 拒绝逻辑应保留；只有经过测试的标准历史重建才能放行，不可简单删掉标志。
- 若接受后才发现无法重建目标历史，应给出可恢复的等待/选择处理，不把界面留在“切换成功”，也不静默用旧模型继续执行。继续原模型需要用户明确选择。
- `ContextService._summary_profile()` 当前读固定快照。建议无显式独立摘要策略时，摘要调用跟随本次上下文准备所绑定的模型；有独立摘要模型时明确展示与记录 `purpose=context_summary`，不能偷偷产生用户不知道的旧模型费用。一次摘要调用的重试仍固定绑定，不中途换模型。
- 已启动的子 run 不随父 run 追溯修改；新委派默认继承父 run 创建它时的有效选择，并冻结为子 run 初始配置。模式与权限上界始终继承收窄。
- 预算余额、审批、项目根目录及工具清单不因切换重置。切换到更贵或价格未知的模型仍遵循现有预算策略，未知价格不能当作零。

## 5. 有序图片上传与原生多模态（反馈 4）

### 5.1 消息结构：顺序只能有一个事实源

沿用已有 `ContentPart`，将前端草稿由 `string + attachments[]` 改为有序 block 文档。推荐先实现轻量块编辑器；只有确有复杂富文本需求时再引入重量级编辑器依赖。

```ts
// proposed composer representation; not an existing project type
// artifactRef is exactly the reference returned by the upload API.
type DraftBlock =
  | { id: string; type: "text"; text: string }
  | {
      id: string;
      type: "image";
      state: "uploading" | "ready" | "failed";
      artifactRef?: ArtifactRef;
      alt?: string;
    }
  | { id: string; type: "file_reference"; artifactRef: ArtifactRef; label?: string };
```

发送结果必须保留 `text → image → text → image` 的顺序。`attachment_refs` 只保留为资源归属/兼容索引，不承担消息位置；不得把所有图片统一追加到正文末尾。

粘贴、拖入和选择图片时，在当前光标位置立即插入带稳定 block ID 的上传占位。上传异步完成后替换该占位，而不是插入到完成时的光标位置；用户已经删除占位时不得让完成回调复活图片。按会话分别保存未提交草稿，处理撤销、重排、重试及部分上传失败；图片未完成上传时阻止发送并解释原因。

### 5.2 提交、引用与兼容

在 `Scheduler.submit()` 中统一收集 `content_parts` 内的 `artifact_ref` 和旧 `attachment_refs`，校验真实存储记录与完整引用一致性、owner、workspace、大小、hash、MIME，并为这些资源登记 run 引用，防止垃圾回收或备份遗漏。资源索引可按 artifact_id 去重，但**正文中用户有意多次插入同一图片的位置不能被去重**。

现有 worker 以 `content_parts` 构造 HumanMessage，仅传附件索引不等于模型一定能看到附件。旧客户端只有 `attachment_refs` 时，兼容层可按提交顺序补齐末尾的 file/image 内容块，并只补充正文未表示的引用；标明这是旧格式兼容，不应影响新格式显式位置。

在提交事务前尽早做视觉能力预检，让用户保留可编辑草稿，而不是让 run 入队后才以 `vision_unverified` 失败。ContextService 与 ModelGateway 的校验仍保留，防止绕过客户端。

### 5.3 消息渲染与预览

将 `App.tsx` 的 `MessageContent()` 拆为按 `content_parts` 顺序渲染的组件：文字块继续使用现有安全文本 / Markdown 规则，图片块显示缩略图、替代文本、上传或失效状态，文件块显示引用卡片。保留工具结果的结构化展示。历史消息的展示不受当前所选模型是否支持视觉影响。

复用现有 `GET /api/artifacts/{id}?disposition=preview`，补齐图片实际类型验证、`X-Content-Type-Options: nosniff`、大小和解码像素上限。当前上传将浏览器传入的 content type 写入记录，因此仅检查声明 MIME 不够；必须核验文件签名并用受限解码器检查图片有效性。拒绝 SVG/HTML 伪装图片和解压炸弹；GIF 动画、多帧及特殊格式仅在适配器明确支持并有约束时放行。

继续使用配对会话认证、owner/workspace 访问边界、同源资源策略，不生成带凭据的分享 URL，不允许任意外部 URL 经本机服务代理读取。前端对象 URL 在删除、替换或卸载时释放；失效资源显示占位而不是悄悄消失。

### 5.4 视觉能力验证与预算

`ContextService._attachments()` 已支持受控图像引用转为 LangChain 标准图片块，无需把图片改成 OCR 文本来冒充原生多模态。应针对当前 adapter / API mode / model 的组合验证实际请求格式，不按供应商名称批量宣称支持。

新增可选视觉探测：使用宿主内置的小型无敏感信息图片和可检查问题，验证图片内容确实参与响应；它是能力协议测试，不是通用视觉准确率评测。验证结果绑定模型配置与适配器版本，在新不可变 ModelProfile 中记录 `vision` 证据；已有正在运行的 profile revision 不原地变更。

当前默认模型设置只认证 text/tool_calling/streaming，且通用验证报告将 vision 列为未验证，因此还需要给用户提供明确的“验证图片能力”入口。为避免加重反馈 6，视觉探测不能强制阻塞所有模型的保存。

必须同时修正 `ContextService` 与 `ModelGateway.estimate_tokens()` 的估算链路：当前序列化字节算法会把 Base64 当成文字成本，造成图片虽然满足 10 MiB 文件限制却被预算拒绝。文字、工具 schema 和图像成本应分开计算；图像依据尺寸、图块/detail 和供应商适配规则估算，没有准确规则时使用标明来源的保守上界，不能算成零。客户端可提示压缩或降低分辨率，但不能未经说明替换用户原图。

大 Base64 不应进入消息公开投影、日志、追踪或重复的原始档案。保留授权 artifact 引用作为长期存储结构，只在必要的提供方请求物化阶段生成图片载荷。压缩/摘要不得伪造“已理解图片”的文字；当前明确保留图片但不做视觉摘要的语义应延续。

## 6. MCP 文件与 Web JSON 编辑器（反馈 5）

### 6.1 事实源与文件位置

建议新增 `settings.data_dir / "mcp.json"`。在默认 Windows 配置下为 `%LOCALAPPDATA%\LocalAgentHarness\mcp.json`，页面展示服务器返回的真实路径。运行配置属于数据目录，不默认写入当前分析项目根目录，避免与源代码、多个项目或版本控制混淆。

用户配置文件是**期望配置的事实源**；数据库保存已校验的生效投影、版本、授权记录和运行快照。Web 编辑的是这个文件的原始 UTF-8 文本，不是从单条 `config/mcp` 记录拼出来的近似 JSON。必须分别展示“文件内容已保存”“配置已加载”“连接/工具目录已验证”，不能混成一个成功状态。

### 6.2 文件格式

采用 Mi-Harness 自己定义并版本化的文档结构，示例如下。`mcpServers` 是本产品选择的用户配置容器，不是 MCP 线协议强制的格式。

```json
{
  "schema_version": 1,
  "mcpServers": {
    "local-tools": {
      "enabled": false,
      "display_name": "本地工具服务",
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "my_mcp_server"],
      "allowed_capabilities": ["tools"],
      "protocol_policy": "auto",
      "connection_scope": "call",
      "connect_timeout": 10,
      "request_timeout": 60
    }
  }
}
```

该示例仅演示结构，不会安装或执行示例命令。`enabled` 属于新增外层配置；编译器剥离后将条目映射到现有 `ConnectionProfile`。`server_id` 由 map key 产生，`config_revision` 由服务器生成，不能由编辑器任意回退。

复用 `ConnectionProfile` 对 stdio / streamable_http / legacy_sse、URL、cwd、超时及凭据引用的约束。HTTP 使用现有 `endpoint` 字段；凭据使用 `credential_ref` / `environment_refs` / `oauth_ref`。严禁为兼容别的产品而把明文 `apiKey`、Authorization header 或秘密 `env` 直接写回文件。

其他产品的 `type`、`url`、`env` 等字段需要显式导入转换和错误提示，不宣称任意 MCP 客户端配置都能直接复制。未知字段不能静默丢弃。严格 JSON 第一版不支持注释和尾逗号；拒绝重复 key，避免编辑器与后端解析结果不一致。

### 6.3 编辑接口与体验

建议新建 `harness/mcp/config_file.py` 与 `McpJsonSettings.tsx`，MCP tab 默认进入 JSON 编辑，而非通用配置表单。

| 建议接口 | 行为 |
| --- | --- |
| `GET /api/mcp-config/file` | 返回 `text`、受控路径、content hash / ETag、生效版本、最近加载错误 |
| `POST /api/mcp-config/validate` | 解析并验证草稿，返回行列/JSON 路径错误；不启动进程、不访问 MCP 网络端点 |
| `PUT /api/mcp-config/file` | 接收原文和 `expected_hash` 或 If-Match，验证后原子替换文件，再协调加载 |
| `POST /api/mcp-config/reload` | 重新读取受控文件并尝试加载；可与文件观察器共用同一服务 |

编辑器提供行号、语法提示、格式化、未保存标记、加载状态、差异核对和错误定位。格式化只在用户操作时执行，不自动重排用户文件。文件 hash 冲突返回 412，配置 revision 冲突返回 409；保留网页草稿并展示磁盘版本，不能静默覆盖。

不接收前端传来的任意文件路径。路径由服务器配置确定，并拒绝链接/reparse point 等绕过受控路径的情况。继续校验配对身份与 CSRF。编辑器文本绝不能包含凭据明文，脱敏占位符也不能作为真实值回写。

### 6.4 文件 / 数据库一致性

不要声称文件替换与 SQLite 提交天然原子。建议采用“受控写入 + 可恢复协调”的方式：

1. 获取跨进程锁，读取当前文件 hash；校验 expected_hash。仅有 `asyncio.Lock` 不足以覆盖 API、worker 和外部编辑器。
2. 对全部条目完成语法、schema、引用与安全检查，生成候选配置版本和写入日志；禁止一半条目新、一半条目旧。
3. 在同目录写临时文件并 flush / fsync，随后 `os.replace`。在支持的平台同步目录元数据，并按 Windows 文件占用情况返回可重试错误。
4. 在数据库事务中发布完整生效投影、版本与源文件 hash，更新写入日志。崩溃恢复通过文件 hash 和日志确定“尚未替换 / 已替换未投影 / 已完成”，重新编译或完成提交。
5. 有错误时保持最后已知有效配置；页面同时显示磁盘草稿错误与旧生效版本。恢复服务不能在用户继续外部编辑后，拿过期备份覆盖新文件。

外部编辑器通常不遵守宿主锁，因此 watcher 还必须处理多次写入、半写文件和保存时 rename：短时去抖、稳定读取、hash 比较、失败重试。不要只依赖 mtime。外部更改在有效校验后才成为生效投影；无效文件保留原文供用户修复。Web 存在未保存草稿时只提示外部更新，不自动覆盖草稿。

旧 `PUT /api/config/mcp/{id}` 应弃用，或在兼容期转发到同一个文件服务并使用同样的 hash / revision 协调；不能同时保留一条独立写数据库的配置通路。

### 6.5 生效、授权与历史运行

修改文件只声明配置，不构成命令执行或出站授权；打开页面、格式化和语法验证不应启动 MCP 服务。连接诊断与启用是明确操作，沿用现有平台权限和服务授权。新服务或命令/端点发生变化时需要重新核对信任，文件不能用 `trusted: true` 自行授予权限。

取消 Agent 编辑后，宿主从“已启用、已授权、目录验证匹配当前配置版本”的 MCP 集合组装默认 Agent 的新 run 快照。配置已保存但目录未验证的服务应显示为待验证，不自动让所有新对话失败，也不能悄悄当成已可用工具。

已运行任务仍使用冻结的 MCP profile 与 catalog。正常编辑用于后续 run，不能把旧会话中的同名工具换成新 command。显式撤销授权属于另一条即时安全控制：阻止之后的调用，但已发出的有副作用请求仍需按现有 unknown / reconciliation 语义处理。

备份、恢复与诊断应纳入 `mcp.json`、已生效 hash 和恢复日志，凭据库仍保持现有独立边界。恢复到新机器后，失效凭据引用显示需要重新绑定。

## 7. 模型验证与保存提速（反馈 6）

### 7.1 已确认的耗时来源

`ModelSetupService._probe()` 创建临时 gateway，在 45 秒总超时内先执行工具验证，再可选执行最多 8 秒的 streaming 检查。`ModelGateway.verify()` 的正常工具验证路径包含：文字请求、产生 fixture tool call 的请求、回传 fixture tool result 的请求。再加 streaming，单次最多四次模型请求。

`test()` 与 `save()` 分别调用 `_probe()`，测试结果没有可复用凭证。因此用户先测试再保存，在完整成功路径上最多会触发八次请求。**这是源码路径分析，不是实际测得的等待时间；不能简单声称所有用户都会等 90 秒。**

此外每次 `_probe()` 都创建并关闭 gateway，不能复用前一次测试的服务对象和验证结果。当前 `_probe()` 已将 gateway 的 `max_attempts` 设为 1，不能把这里误诊为默认三次重试导致；是否存在提供方内部重试、DNS、凭据库或事件循环阻塞，需要独立测量。

### 7.2 先消除重复验证，再优化复杂调度

第一阶段无需先引入大型任务系统。最小有效改造是：测试返回服务器验证凭证；未修改配置时保存复用结果；将可选 streaming / vision 验证移出保存必经路径。

| 操作 | 推荐语义 |
| --- | --- |
| 保存配置 | 仅完成本地 schema、版本、凭据写入和配置持久化；可以保存为未验证草稿，不自动激活 |
| 快速测试连接 | 一次最小文字调用；只证明相应连接/文字能力，不能宣称工具和视觉已验证 |
| 验证 Agent 能力 | 执行有界文本、工具调用及工具结果配对检查，产生可复用结果 |
| 保存并使用 | 匹配有效验证凭证时快速发布并激活；没有有效凭证则明确进入验证过程 |
| 检查 streaming / vision | 单独的可选能力检测，可发布新的 profile revision，不阻塞所有模型保存 |

不建议为节省一次请求直接合并/削弱必需的多轮工具配对检测，也不能用 `/models` 返回成功代替推理可用性检测。用户只改模型显示信息等非推理字段时，不应无理由重新请求供应商。

### 7.3 验证凭证与缓存

验证凭证由服务器签发或使用不可猜测 ID 引用服务端记录，不接受客户端自报 `verified=true`。建议缓存键包括：owner / 配对安全域、provider、规范化 endpoint、model_id、adapter/API mode 与契约版本、关键模型参数、凭据版本或受控 HMAC 指纹、验证 suite 版本。验证有效期可先设为 10 分钟作为产品参数，再根据数据调整。

保存时重新核对配置 fingerprint、expected_revision、凭据绑定、授权、维护状态和凭证有效期，匹配才允许复用。更换 Key、endpoint、模型或协议参数使相关验证失效；改变一个无关展示字段不应失效。缓存失效不能沿用旧的视觉或工具认证。

新增临时模型 ID 应在表单建立时分配稳定的 setup draft ID，或使用与发布 ID 无关的受控配置 fingerprint，避免每次 `_prepare()` 生成新 ID 导致缓存永远未命中。

同一 owner / fingerprint 的并发测试合并为一个 in-flight 请求（single-flight）；失败和取消只做短期、明确状态的去重，不能把一次偶发超时长期缓存成永久不可用。缓存中不得保存明文 API Key；缓存元数据写日志前去除敏感内容。复用既有 model-setup save 路由对秘密只持久化指纹的做法，不把原始请求交给通用命令存储。

### 7.4 分阶段进度与有界异步

进一步优化可将验证建模为任务，返回 `verification_job_id` 并通过查询或 SSE 展示：排队、连接/文字、工具调用、工具配对、完成。每个阶段给出实际状态和耗时，不播放虚假的百分比。

任务需要总超时、取消、owner 归属、并发限制和服务重启后的确定状态。新输入的秘密只在受控内存或凭据库中短暂存在；服务重启后不能恢复明文的临时任务应标记为中断，要求重新输入或使用已保存凭据。不要创建无法追踪的后台协程持有 Key 无限运行。

远程模型请求不应占用 SQLite 写事务。对 DNS、系统凭据库等可能阻塞的操作先加阶段埋点；确认阻塞后再放到受控线程执行，不把一个线程绑定的数据库连接随意跨线程共享。连接池可以按 owner / endpoint / credential fingerprint 安全复用，但必须维持 ModelSetupGrant 的端点专用授权，不扩大到 MCP、浏览工具或通用网络。

### 7.5 性能指标：目标而非现状

增加 `model_setup.phase` 指标，记录 queue、prepare、network/probe、credential_write、db_publish、total；只记录阶段名、状态、duration、匿名关联 ID 和请求计数，不记录提示原文或秘密。

| 场景 | 建议验收目标与限制 |
| --- | --- |
| 展开会话模型列表 | 零供应商请求；在约定本地测试环境中 p95 小于 200 ms |
| 相同配置已验证后保存 | 零额外验证请求；凭据库正常时本地保存 p95 目标小于 1 秒 |
| 快速连接检测 | 一次推理请求；UI 立即显示阶段，可配置 5–10 秒超时；慢启动本地模型可另设较长超时 |
| 完整 Agent 验证 | 文本/工具/配对最多三次必需请求，可选 streaming / vision 不放在保存关键路径 |
| 验证中止 | 可取消，释放并发槽；迟到结果不能覆盖用户后来编辑的模型草稿 |

真实供应商生成、网络、限流和 Ollama 冷启动无法由应用保证固定耗时。优化报告应同时列出固定延迟 mock 下的结果和供应商实测分布，不混为一个平均数。

## 8. 文件级任务清单

“新增”表示建议建立的新文件；路径可在实现评审中调整，职责不能遗漏。

| 文件 / 模块 | 修改指导 |
| --- | --- |
| `apps/web/src/features/workbench/App.tsx` | 移除 Agent 选择状态；接入模式、模型、会话偏好；按 block 生成提交；避免所有逻辑继续堆在主组件 |
| 新增 `features/workbench/ModeSelector.tsx`、`ModelSelector.tsx`、`Composer.tsx`、`MessageContent.tsx` | 单一模式入口、可搜索模型下拉、有序上传编辑、图文渲染 |
| `apps/web/src/features/settings/Settings.tsx` | 去除 Agent CRUD；MCP tab 交给文件编辑组件；保留权限与技能入口 |
| 新增 `apps/web/src/features/settings/McpJsonSettings.tsx` | JSON 原文、hash 冲突、错误定位、文件与生效状态 |
| `apps/web/src/features/settings/ModelSettings.tsx`、`model-setup-state.ts` | 分离保存/验证/激活；验证 fingerprint 与取消状态；减少重复 discover/test 请求 |
| `apps/web/src/api/client.ts`、`api/types.ts`、事件流与 run reducer | 新增控制和配置文件 API；SSE 断线恢复；区分 desired/effective 和真正消息归属 |
| `harness/server/schemas.py`、`app.py` | 模式、偏好、切换、MCP 文件、验证凭证契约；关闭公开 Agent 写入口；加图片校验 |
| `harness/server/composition.py` | 新提交根据明确模式/模型组装快照；单一助手投影；默认配置与会话偏好解耦；统一 MCP 生效配置 |
| 新增 `harness/runtime/modes.py` | 受控模式策略与工具/权限上界，不提供任意用户可编辑的 Agent 模板 |
| 新增 `harness/runtime/model_selection.py` | 指令、CAS、调用绑定、恢复、模型选择状态机 |
| `harness/runtime/engine.py` | 在模型调用边界复用 `ModelGateway.switch()`，override 新 handle；保持唯一 create_agent 循环 |
| `harness/scheduler/service.py`、`worker.py` | 模型指令接受/终态竞争；内联附件引用校验登记；跨进程恢复；真实模型归属传递 |
| `harness/model_gateway/gateway.py`、`protocol.py`、`profiles.py` | 复用 switch；能力/历史兼容；每调用绑定与审计；多模态预算及独立视觉证据 |
| `harness/context/service.py` | 对目标模型重建上下文；摘要模型选择规则；图片估算与 artifact 引用保持 |
| `harness/tools/gateway.py`、`harness/policy/engine.py` | Plan 执行期强制收窄、子任务继承、MCP 信任与撤销，不能只过滤 UI |
| 新增 `harness/mcp/config_file.py` | 原始文件读写、编译、schema、原子替换、hash、恢复协调；不绕过 ConnectionProfile |
| `harness/mcp/models.py`、`server/composition.py` | 外层 JSON 文档到现有 profile 的转换；目录版本匹配；旧单记录 API 兼容统一入口 |
| `harness/server/model_setup.py` | 验证复用、阶段化、single-flight、可选探测、凭据与发布回滚 |
| `harness/storage/store.py`、`backup.py` | 新记录及迁移、控制事件、MCP 文件/日志备份恢复；不覆写历史快照 |
| `README.md`、`docs/implementation/`、`docs/compatibility/mcp.md` | 更新操作说明、模式限制、模型热切换规则、JSON schema 和迁移行为 |

API 类型使用现有生成链路更新，不手工修改 `apps/web/src/api/generated/schema.ts`：

```powershell
uv run python apps/web/scripts/export_openapi.py
npm --prefix apps/web run generate:api
npm --prefix apps/web test
npm --prefix apps/web run build
uv run pytest -q --junitxml=docs/verification/latest-pytest.xml
```

上述为实施后的建议验证命令，本次文档分析没有执行它们。

## 9. 数据迁移与兼容

### 9.1 会话与 Agent

采用前向、幂等迁移。没有模式的旧会话默认 `react`；没有显式模型偏好的会话，优先使用其原 Agent 的有效模型配置作为迁移候选，候选不可用则提示用户选择，不静默改成另一云端模型。明确记录偏好是在迁移时冻结还是随后跟随默认值，避免启动顺序改变结果。

历史 run、审批、AgentSpec、模型 profile 与 checkpoint 不改写。旧自定义 Agent 的历史仍可回看和按原冻结快照恢复；新提交统一使用受控公开助手。权限迁移采用不扩大原则，保留旧权限上界并提示行为差异，不能因为默认 Agent 工具更多就扩大旧会话权限。

旧提交中的 `agent_spec_revision` 在兼容期仍校验；新客户端由服务器解析内置 Agent revision，不要求用户理解它。不要把新模式强行塞到旧 `agent_spec_id` 中。

### 9.2 MCP

仅当受控 `mcp.json` 不存在时，导出现有 MCP 配置；已有文件优先校验，不覆盖。迁移保留凭据引用、原配置历史以及运行快照；文件 version 与服务器 config_revision 的映射必须明确。

一次性迁移的“已完成”记录与候选文件 hash 关联；中断重试可恢复，不能导出一半服务。支持向前升级 schema，拒绝静默降级或丢失未知字段。

### 9.3 回滚

将“单 Agent UI”“会话模型选择”“运行热切换”“图文编辑”“MCP 文件配置”“验证复用”拆成可独立关闭的功能开关。关闭热切换入口后，已接受的指令仍需由支持该协议的 worker 处理；不要回滚到不认识新状态的 worker 去恢复新运行。

旧版本无法安全理解的新 run 应阻止恢复并提示版本要求。不要通过删除新记录、重置 checkpoint 或强制把 unknown 工具状态改成功来回滚。

## 10. 验收用例与测试设计

以下是待新增/扩展用例，不表示已有测试通过。先使用确定性 fake provider 与可控时间/故障点，不依赖真实服务验证并发语义。

| 编号 | 用例 | 必须断言 |
| --- | --- | --- |
| A01 | 新建会话、侧栏快速新建、刷新 | 不出现 Agent 列表；只有 ReAct / Plan；偏好恢复正确 |
| A02 | 旧客户端直接写 Agent 配置或自选内部 ID | 后端拒绝；不是仅隐藏按钮 |
| A03 | Plan 请求写文件、命令执行、危险 MCP、通过子任务写入 | 模型提供工具集与执行网关双重阻断；项目文件无副作用 |
| A04 | 修改计划后点击旧的执行确认 | 版本/hash 不匹配，要求重新确认；具体副作用审批仍保留 |
| M01 | 模型 A 调用工具，工具暂停时选择 B，随后释放工具 | 指令在暂停期间被接受；下一次推理确实由 B 收到完整文字和工具结果，不再调用 A |
| M02 | A 正在流式响应时选择 B | A 的现有流不混入 B 内容；下一逻辑调用归属 B |
| M03 | A 的一次调用发生网关重试，期间选择 B | 原逻辑调用重试继续 A，下一逻辑调用 B；UI 状态与契约一致 |
| M04 | 上下文准备期间 B 改 C | 派发前版本复核生效；不把按 B 预算准备的上下文交给 C |
| M05 | 连续选择、重复幂等键、双标签页 CAS 冲突 | 审计完整，未派发选择可被覆盖；不同正文复用同幂等键被拒绝 |
| M06 | 指令接受后 worker 重启；各派发/checkpoint 故障点恢复 | 不丢指令、不回退全局控制、不重复工具副作用；旧 fencing token 不能写状态 |
| M07 | A 最后一次输出直接结束，或切换与取消竞争 | 不为切换多跑一轮；终态拒绝热切换；已保存偏好用于下一次提交 |
| M08 | 有图片切文字模型、更小窗口、不可移植 continuation、无权限 endpoint | 拒绝或进入明确可恢复处理；不删图、不泄漏私有字段、不静默 fallback |
| M09 | 模型配置同 ID 发布新 revision | 正在运行的选择不漂移；新选择必须明确新版本 |
| M10 | 模型/模式/会话/多分支组合 | 会话之间隔离，不误改历史 run 和其他分支；模式不随模型改变 |
| I01 | 文字 A → 图1 → 文字 B → 图2 | 编辑器、HTTP body、数据库、worker HumanMessage、提供方请求和历史 UI 顺序一致 |
| I02 | 光标移动、占位删除、并发上传完成乱序、拖动重排 | 使用稳定 block ID，不复活已删除图片，不按上传完成时间改变位置 |
| I03 | 同图插入两次、仅图消息、上传失败重试、刷新历史 | 正文重复位置保留；资源索引可去重；失败可修复 |
| I04 | MIME 伪装、超大像素、越权引用、hash 不匹配、SVG | 服务端拒绝；预览不执行主动内容；run 入队前给出可用错误 |
| I05 | 常见图片在预算范围内、超预算图片、历史图片切模型 | 不把 Base64 当文字；真实提供方 usage 与估算分开记录 |
| C01 | Web 保存 JSON 后磁盘检查；外部修改文件后 Web 重载 | 两侧看到同一原文/hash，生效版本可追踪 |
| C02 | 非法 JSON、重复 key、未知字段、半写文件 | 显示准确错误，保持最后有效投影，不部分发布 |
| C03 | 同时 Web 保存与外部编辑、文件替换后数据库前崩溃 | 返回冲突或按日志恢复；不丢新文件、不虚报全部生效 |
| C04 | 仅查看/格式化/验证 MCP，或 JSON 声明 trusted | 不执行 MCP 命令，不产生额外授权 |
| C05 | 活动 run 期间编辑/删除/撤销 MCP | 正常编辑不篡改冻结连接；撤销阻断后续调用；未知副作用不自动重做 |
| P01 | 完整测试后不改配置直接保存 | 保存阶段供应商验证请求计数为零 |
| P02 | 修改模型、endpoint、Key、关键参数后保存 | 原验证凭证失效；未验证配置不能激活 |
| P03 | 并发测试同一配置、任务取消、过期、服务重启 | single-flight 有效；迟到结果不覆盖新草稿；秘密不出现在命令/日志中 |
| P04 | mock 固定各阶段延迟与凭据库失败 | 阶段耗时可定位，事务回滚，默认模型不误切换 |

前端测试补充键盘选择、焦点管理、移动端下拉定位、图片替代文本和错误反馈。运行控制测试应接入真实的持久化 checkpointer/worker 组合，不能只模拟一个 React 下拉框就算热切换验收。

真实供应商联调应至少覆盖项目实际支持的 OpenAI / Anthropic / Ollama adapter 中各一个可用配置；具体模型由测试环境提供，不在测试代码硬编码某个供应商所有模型都支持图片。记录 adapter 版本、API mode、能力证据与日期，失败按能力维度说明。

## 11. 推荐交付顺序

### 阶段一：模型验证去重与观测

先加入请求计数/阶段耗时和验证凭证复用，将可选 streaming 从保存关键路径移走。保持旧接口兼容，证明“测试后保存不重复请求”。这是最直接改善等待体验且较独立的改动。

### 阶段二：单 Agent、模式、静态会话模型选择

完成单一公开助手与后端只读限制、会话偏好、初始提交模型绑定以及 Plan 执行期权限。先确保新一轮对话能准确选择模式和模型，再开放运行中入口。

### 阶段三：持久化热切换与有序图文

实现调用绑定、恢复和实际模型归属；通过 M01–M10 后开放运行中切换。图文编辑在引用校验、视觉证据和预算测试完整后启用；两者交叉场景必须联合验收。

### 阶段四：MCP 文件配置与完整迁移

发布文件服务、Web JSON 编辑器、外部修改检测、崩溃恢复、旧 API 转接和备份迁移。只有端到端校验确认旧运行不受影响、配置不双写漂移后，才下线旧记录编辑 UI。

每个阶段单独提交、单独回归，避免把模型、Agent、权限和存储迁移揉成一个无法审查的大改动。验收以行为和持久化证据为准，而不是“页面已经出现按钮”。

## 12. 外部研究依据

以下为本次查阅的官方资料。这里只借鉴与需求相关的概念与接口；产品页面内容可能变化，Mi-Harness 的实现必须按本项目锁定依赖验收。

- **[R1] LangChain Models / Agents**：[Models](https://docs.langchain.com/oss/python/langchain/models)、[Agents](https://docs.langchain.com/oss/python/langchain/agents)。参考统一模型接口与模型调用 middleware 的动态选择机制。项目已有 `request.override()` 和 `ModelGateway.switch()`，因此优先接线而非更换循环框架。
- **[R2] OpenAI Codex IDE**：[官方 IDE 页面](https://developers.openai.com/codex/ide/features)。参考会话输入区附近的模型入口与就地交互，不把页面展示等同于已经验证运行中热切换语义。
- **[R3] CodeBuddy Models**：[models.json 配置说明](https://www.codebuddy.ai/docs/ide/Features/models)。参考可见模型列表与能力标识的组织方式；不照搬其明文 Key 示例，本项目继续使用系统凭据库。
- **[R4] CodeBuddy Plan / MCP**：[Plan Mode](https://www.codebuddy.ai/docs/ide/Features/Plan-Mode)、[MCP 配置](https://www.codebuddy.ai/docs/ide/User-guide/MCP)。参考先规划再确认的交互、JSON 配置入口；具体只读约束与本项目文件 schema 由宿主定义。
- **[R5] MCP Tools 规范**：[2025-11-25 Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。工具 annotations 不能无条件信任，应保持用户可控和宿主权限检查。引用该版本说明安全原则，不据此宣称它是最新版本，也不要求本次升级 MCP 协议版本。

---

**完成标准：用户能在同一助手中选择模式与模型；运行中的下一次逻辑模型调用确实使用新选择且可恢复；图片位置从编辑到提供方输入保持一致；MCP 网页与磁盘配置一致且安全；测试后保存不再重复进行完整模型探测。**
