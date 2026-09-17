# 11 · Web 工作台实现指导

[实现索引](README.md) · [公共契约](01-contracts.md) · 前置：[HTTP 与事件协议](10-api-events.md)、[权限与本地服务](12-platform.md)

本文唯一维护前端页面、组件、视图状态和用户旅程。所有 HTTP 路径、事件字段和恢复协议直接引用 10，不在此复制。
前端呈现服务端事实；不自行判断授权、不执行任务状态转换，也不以浏览器连接状态推断 run 是否停止。

## 1. 交付范围与模块

P0 提供单用户本地工作台：工作区/会话、对话、工具时间线、审批、上下文、产物与设置诊断。
P1 增加任务树、会话分支、运行中 steering、记忆管理、MCP 授权和补充输入；入口受服务端能力开关控制。
使用 React + TypeScript；生成 API client 放入只读生成目录，业务组件不直接拼接 URL。

| 目录 | 职责 |
|---|---|
| `apps/web/src/api/generated/` | 从 10 的 OpenAPI 生成 client/types |
| `apps/web/src/api/event-stream.ts` | SSE 连接管理、解析和受控重连 |
| `apps/web/src/state/run-reducer.ts` | RunView 与事件投影，不直接执行网络请求 |
| `apps/web/src/features/workbench/` | 布局、对话、输入和运行控制 |
| `apps/web/src/features/interactions/` | 审批/补充信息表单及提交回执 |
| `apps/web/src/features/inspectors/` | 工具、上下文、产物、任务树 |
| `apps/web/src/features/settings/` | 模型、Agent、Skills、MCP、策略和诊断 |

## 2. 页面与组件职责

默认三栏布局：左侧工作区和会话，中间对话与执行时间线，右侧按需打开检查面板。
窄屏先保证对话和审批可操作，导航与右侧面板使用抽屉，不压缩批准内容到无法阅读。

| 页面/组件 | 内容与动作 |
|---|---|
| `WorkspaceSwitcher/SessionList` | 当前工作区、会话标题、重命名/归档、最近活动和等待用户标记 |
| `ConversationView` | 已提交消息、临时输出、引用、排队与恢复提示 |
| `Composer` | 输入、附件、Agent/Skill 选择、发送中的本地回执 |
| `RunHeader` | 服务端状态、取消按钮、重连标记和最近更新时间 |
| `ToolTimeline/ToolDetail` | 工具来源、参数摘要、耗时、尝试、结果和产物入口 |
| `ReconciliationPanel` | 结果未知的原因、现有证据、核对材料与服务端允许的处理动作 |
| `InteractionCard` | 具体操作/表单、目标、内容或 diff、授权范围、有效期 |
| `ContextPanel` | token 预算、估算说明、来源、激活 Skill、压缩点和原始档案 |
| `ArtifactPanel` | 名称、版本、来源、关联操作、受控预览/下载 |
| `TaskTree` | P1：目标、父子关系、状态、预算和结果 |
| `MemoryManager` | P1：记忆列表、来源/范围/有效期、待审提议、编辑和遗忘回执 |
| `SettingsShell` | 配置版本、启停/测试/刷新、错误定位和更改回执 |

普通界面使用“排队中、运行中、等待确认、正在恢复”等清晰文案；完整枚举由 01 映射，不能再发明持久状态。
完整 SDK 异常、协议字段和插件注册细节放入诊断面板，不挤占对话流程。

## 3. 前端视图模型

```text
WorkbenchState
  selectedWorkspaceId, selectedSessionId, selectedRunId
  runsById: RunProjection
  draftsBySessionId: ComposerDraft
  pendingCommandsByKey: PendingCommand
  panelSelection, connectionByRunId

RunProjection
  snapshotRevision, lastAppliedDurableSeq
  committedMessagesById, transientStreamsById
  toolCardsByOperationId, interactionsById
  artifactRefsById, childRunRefsById
  serverStatus, usageSummary, contextSummary

PendingCommand
  idempotencyKey, submittedBodyHash, localPhase, receipt?, error?
```

这些是 UI 投影，不是新数据库实体；消息和运行字段复用生成类型。
`localPhase` 只用于“发送中/待重试”等界面反馈，不能写回服务端 RunStatus。
内存保存未提交 delta；持久浏览器缓存若启用，应按 owner/workspace 隔离并在注销时清理。
默认不把密钥、审批完整材料和工具原始输出写入 localStorage。

## 4. Reducer 与请求协调

1. 初始导航读取服务端 snapshot，按其耐久游标建立事件订阅；清理上一 run 的订阅和未提交显示。
2. 对耐久事件按 10 的序列协议处理，重复事件不重复插入消息、工具卡片或通知。
3. 检测序列缺口时暂停投影推进并请求恢复；只在成功合并后记录新的 lastAppliedDurableSeq。
4. 临时文本按 stream_id/chunk_index 累加；收到同 message_id 的完整消息后移除临时展示。
5. 状态卡、工具卡和交互卡按 ID/revision 更新；旧响应不能覆盖新状态。
6. snapshot 重建时用服务端事实替换旧投影，未提交草稿单独保留；未完成临时文本标记中断。
7. 网络断开只改变 connection 状态并显示“正在重新连接”；取消按钮另行提交明确业务命令。
8. 提交协调器生成一次幂等键并保存原正文；超时重试复用该键，编辑正文后必须生成新键。
9. revision 冲突读取最新视图后展示差异；不能自动修改审批参数并静默再次提交。
10. 不把点击批准直接渲染成工具成功；先展示受理，再等待服务端事实事件。

Reducer 保持纯函数，网络与计时逻辑在 effect/service；事件流重放绝不能触发发送、批准或工具调用。
SSE 超时、事件过期、未来游标等协议处理以 10 为准；前端只实现规定分支，不能“忽略错误继续追加”。

## 5. 关键用户旅程

**首次启动**：兑换启动凭证 → 选择工作区 → 选择模型提供方、输入 API Key、选择模型名称 → 保存并使用 → 创建会话。
失败要显示所在步骤及可修复项，模型“连接成功”和“多轮工具能力通过”分别展示。

**提交任务**：输入目标/附件并选 Agent 或 Skill → 显示本地提交状态 → 接收回执 → 呈现队列/执行进度。
附件先完成上传并取得受控引用，再允许随任务发送；失败时保留草稿并显示大小/类型等可修复原因。
任务运行中普通发送仍产生排队的新任务；P1 steering 用单独入口和说明，避免一条内容被消费两次。

**审批与补充信息**：定位具体交互 → 展示完整审阅材料 → 校验表单 → 提交答复 → 显示恢复后的运行事实。
允许一次、拒绝、有限范围授权应清晰区分；不能默认勾选永久允许。
MCP 补充输入与系统权限审批使用不同标题和说明，避免用户把协议确认理解为文件/命令授权。
交互过期或已被其他页面处理时，禁用旧表单并更新状态；不自动答复新的交互。

**关闭后返回**：重新选中会话 → 读取 snapshot → 恢复已提交输出与开放交互 → 继续订阅事件。
浏览器历史中的部分输出如未被服务端提交，显示中断说明；不拼入下一轮上下文。

**未知结果核对**：从需核对的工具卡查看证据 → 提交材料及允许的处理决定 → 展示服务器核验结果。界面按 07 的核对契约展示含义；“接受未决结果”必须显式选择，不能显示成工具成功或默认重新执行。

**产物查看**：从回答或工具卡进入产物 → 展示来源和版本 → 安全预览或下载。
HTML 等主动内容使用隔离预览；Markdown 原始 HTML 及不可信链接按 12 的内容策略渲染。

2026-09-18 实现补充：模型历史消息与临时流式消息共用 `components/MarkdownText.tsx`，依赖固定为 `react-markdown@10.1.0` 与 `remark-gfm@4.0.1`。React 渲染解析后的节点，不使用 `dangerouslySetInnerHTML` 或 raw HTML 插件。绝对 HTTP(S) 链接可点击，其他协议与相对导航降级为文本；远程图片只显示链接，不自动加载。表格和代码块独立横向滚动，代码保留缩进。当前不提供代码语法高亮、数学公式或 Mermaid 图形解析。用户输入、工具消息及产物预览的纯文本语义保持不变。

**P1 记忆管理**：从上下文面板进入记忆列表 → 查看来源、范围、审阅状态与有效期 → 接受/拒绝提议或编辑已保存内容。
遗忘动作展示具体记忆及作用范围，提交后等待服务端回执再移出当前列表；下一次检索使用 04 的最新可用集合。
记忆 revision 冲突先刷新详情并保留编辑草稿；不能仅在浏览器隐藏一条记忆就显示“已遗忘”。

## 6. 设置页分工

模型页采用简洁的“添加模型/编辑模型”表单，主流程只有提供方、API Key、模型名称三项；参照用户提供的 CodeBuddy/WorkBuddy 截图组织间距和操作顺序。提供方有内置地址与协议，API Key 使用密码输入并提供“测试连接”，模型名称由官方预设或实际发现目录选择，也允许输入完整模型 ID。只有自定义兼容服务展开地址，本地无认证服务不强制填写 Key。配置 ID、adapter、API 模式、credential_ref、能力 JSON 与上下文参数不出现在普通表单。

“保存并使用”由服务端执行有限协议检查、存入系统凭据库、发布不可变 profile revision，并更新默认 Agent 的后续任务模型；用户无需再手动创建 Agent 或拼接版本引用。旧任务保留原快照。模型列表展示提供方、模型名称与当前使用状态；测试失败和并发版本冲突有明确回执，不能显示已启用。

编辑已保存模型不回填原 API Key，留空表示保留同一提供方和地址的原凭据；更换提供方/地址不得复用旧 Key。切换提供方、关闭编辑器、保存成功时清理密钥状态，测试结果在表单变化后失效。敏感请求不进入普通 PendingCommand 重试缓存，不写入 localStorage/sessionStorage，不在输入过程中自动发出请求。费用未知仍显示未知，连接通过与工具协议通过分别报告；版本与详细诊断放到按需查看的位置。
Skill 页展示来源、正文、固定 hash、启停、验证诊断和当前 run 的激活版本；刷新不暗示旧 run 已升级。
插件页 P0 展示内置注册状态和版本；未实现第三方隔离前不提供误导性的“安装任意插件”入口。
P1 提供静态声明清单导入与指定版本排空：先展示完整清单供审阅，再提交幂等写入并展示服务端回执。当前宿主仅支持 Skill 来源、静态 MCP 连接配置和 Agent 模板；目录来源保持不可信，声明不会直接授予权限。任意可执行 entrypoint 及宿主未实现的扩展类别明确拒绝。排空只停止新绑定，已有任务仍使用固定版本；界面不能把排空受理显示为已经卸载。
MCP 页展示配置目标、认证状态、真实协议时代、目录完整性、工具来源和分层诊断结果。
MCP 配置导入先提供可审阅摘要；“连接成功”不能替换“示例工具执行成功”。
策略页展示请求范围与实际有效权限，权限计算结果来自服务端；前端不能由 Skill/工具提示自行推导授予。
各页保存均显示 revision 与明确回执，冲突时保留用户编辑内容供重新核对。

## 7. 实施顺序与交付证据

先用类型化 fixture 完成布局和 reducer，再接真实提交/snapshot/SSE，然后完成审批、取消和诊断设置。
组件 fixture 覆盖长文本、大工具输出、无费用数据、排队、等待、重连和空状态，避免只制作成功态截图。
所有可操作项支持键盘、焦点顺序和明确禁用原因；长时间线使用分页或虚拟化，不阻塞输入。
交付可启动 Web 工程、生成 client、核心页面截图/录屏、一次真实任务的完整使用旅程记录。
验收规则只在 [13 的 U01](13-verification.md#u01)、[R04](13-verification.md#r04)、[S01](13-verification.md#s01)、[P04](13-verification.md#p04) 维护；本文提交 UI 侧证据。
P1 记忆管理对接 [CX04](13-verification.md#cx04)、[UI02](13-verification.md#ui02)；P0 设置/附件完整旅程对接 [UI01](13-verification.md#ui01)，提交服务端回执与界面变化对应证据。

## 来源与设计追溯

[原系统设计 §14 Web 工作台](../通用Agent-Harness系统设计文档.md#s14)、[§10 Skills](../通用Agent-Harness系统设计文档.md#s10)、[§11 MCP](../通用Agent-Harness系统设计文档.md#s11)。
[DeepSeek Harness 专项调研](../research/deepseek-harness.md) 提供桌面工作台参照；本页面设计不宣称对其视觉或传输实现一比一复刻。
