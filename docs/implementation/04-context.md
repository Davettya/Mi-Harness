# 04 · 上下文、压缩与记忆实现指南

[实现导航](README.md) · [共享契约](01-contracts.md) · 先决：[存储](02-storage.md)、[模型接入](03-model-gateway.md)

本文是 `harness/context/` 的实现依据，唯一维护 ContextView、压缩摘要及记忆对象的模块契约。实现与验收状态以实施计划末尾核对清单和测试证据为准；压缩质量必须通过固定任务集验证。

## 1. 职责与边界

Context Service 为每次模型调用生成受预算约束、可追溯的输入视图，统一调度工具输出卸载、历史压缩与材料检索。
原始消息与产物的保存机制、事务和 CAS 原语引用 [02](02-storage.md)；模型能力和计数器引用 [03](03-model-gateway.md)。
技能内容来自 [08](08-skills-plugins.md) 的不可变快照，MCP 资源来自 [09](09-mcp.md) 的引用，不直接连接外部服务取数。
权限过滤由 [12](12-platform.md) 提供，用户固定事实及记忆编辑入口由 [10](10-api-events.md) 提供。
本模块不持有 run 或 branch 所有权，不更新 run 状态，不把压缩结果当作执行授权。

保持四类数据分离：活跃输入可重建，原始档案可追溯，长期记忆可纠正，checkpoint 用于恢复。
checkpoint 中的工作历史可能已压缩，不能替代用户原文归档；向量索引不能替代原始数据。

## 2. 本模块唯一维护的数据契约

### 2.1 ContextView

| 字段 | 实现要求 |
|---|---|
| `input_view_id` | 一次最终模型输入视图的不可变 ID |
| `source_revision` | `{input_revision, context_revision, pin_revision}`；检测可见输入、摘要指针与 branch 固定内容集合变化；无固定记录时 pin_revision 为 0 |
| `message_refs / material_refs` | 有序消息与材料引用；产物用共享 `ArtifactRef` |
| `selected_tool_refs / tools_schema_hash` | 本轮工具定义引用及规范化 hash |
| `skill_snapshot_refs / summary_refs` | 实际使用的技能和摘要版本 |
| `pin_refs` | 本轮实际纳入的 ContextPin ID；具体集合版本由 source_revision.pin_revision 固定 |
| `model_profile_ref` | [03](03-model-gateway.md) 定义的 profile revision |
| `budget_breakdown` | 输入上限、输出预留、余量、各类别估算和算法版本 |
| `normalized_request_hash` | 按固定规范化规则计算，不包含鉴权字段 |
| `input_snapshot_ref` | 可选的脱敏实际输入快照，用于故障复现 |

ContextView 记录实际选中内容，不把整个资料目录复制进去。
最终请求组装不得在 hash 之后隐式附加技能或工具；确需追加时生成新视图与 hash。
运行身份使用共享 `ExecutionContext`，不与消息内容混在一起。

### 2.2 CompactionSummary

| 字段 | 实现要求 |
|---|---|
| `summary_id / revision` | 不可变摘要记录及版本 |
| `covered_message_refs / parent_summary_refs` | 被替代的完整消息区间和上游摘要引用 |
| `goal / constraints` | 当前目标、用户硬约束及引用 |
| `verified_facts / changes / evidence_refs` | 已验证事实、已做修改及其证据 |
| `failed_approaches / open_items / next_steps` | 失败尝试、未决事项与下一步 |
| `source_manifest_hash / content_hash` | 输入来源集合及摘要内容 hash |
| `model_profile_ref / created_at` | 摘要生成版本与时间 |

摘要用结构化字段保持任务状态，但不能替代审批、预算、子任务或操作记录；这些使用权威结构化对象引用。
正文中的“用户已同意”不具有授权效力，审批内容和有效期由工具与平台模块判断。
MemoryRecord 采用 `id / revision / owner / workspace / scope / kind / content / source_refs / created_at / expires_at / confidence / review_state`，身份、作用域与引用类型沿用共享契约。
记忆明确区分提议与已接受内容；具体写入规则由平台策略决定，不能由模型凭置信度自行授权。

### 2.3 ContextService

```text
compose(execution_context, graph_state_ref, profile_ref, tool_catalog_ref) -> ContextView
compact(execution_context, source_revision, candidate_refs) -> summary_ref
retrieve_memory(execution_context, query, limits) -> authorized_memory_refs
propose_memory(execution_context, content, source_refs) -> memory_proposal_ref
```

`compact` 只提交摘要候选及受版本约束的 active pointer 更新，不覆盖整个 graph state。
供模型使用的消息对象由 ContextView 引用构造，persist 和临时 materialize 明确分开。
`materialize` 重新检查当前 pin revision；若与已保存视图不同，拒绝沿用旧输入，不能在旧 hash 后悄悄追加固定内容。

本地附件通过可注入端口 `artifact_reader(execution_context, ArtifactRef, limit_bytes) -> bytes` 读取，可同步或异步实现。Host 实现必须核验当前 owner/workspace、完整 ArtifactRef 与权威元数据一致，并通过对象仓储进行完整性检查和有界读取；本模块不从任意 URL 或用户路径获取附件。

P1 用户管理使用独立 MemoryService，不要求创建 run：

```text
list(authenticated_identity, workspace_id, filters, cursor, limit) -> memory_page
get(authenticated_identity, memory_id) -> MemoryRecord
review(authenticated_identity, memory_id, decision, expected_revision) -> MemoryRecord
update(authenticated_identity, memory_id, patch, expected_revision) -> MemoryRecord
delete(authenticated_identity, memory_id, expected_revision) -> deletion_receipt
```

`authenticated_identity` 采用 Host 已认证的共享身份标识及当前访问检查，不是由用户传入的授权声明，也不要求伪造 ExecutionContext。
list/get 同时检查作用域和可见性；review 只处理接受或拒绝提议，update 只允许内容、来源补充、时效等明确白名单字段，不能修改 owner/workspace 冒充转移授权。
review/update/delete 要求当前 expected_revision，以仓储 CAS 防止旧页面覆盖新记录；索引与缓存失效必须跟随已提交版本，HTTP 映射由 [10](10-api-events.md) 定义。

## 3. 按顺序实现 Composer

1. 读取当前输入版本、原始消息引用、有效配置快照和模型 profile。
2. 优先组装系统规则、Agent 指令、当前任务与最新用户修正。
3. 加入结构化运行事实的最小投影：未决事项、必要证据和完整未完成工具配对。
4. 选择本轮工具 schema、Skill 快照、已授权记忆与检索材料。
5. 加入近期完整回合与生效摘要，保持供应商要求的角色及 provider metadata。
6. 对最终请求整体估算；触发卸载/压缩后重新估算，确认未超硬上限。
7. 保存 ContextView 并返回引用；ModelGateway 只调用该视图所描述的请求。

外部材料包含来源、版本与信任标签；标签辅助解释，不替代执行权限检查。
用户新修正优先于冲突的旧记忆；保留冲突来源便于追查，不把冲突悄悄融合为新事实。
工具调用与结果作为整体选择，不能只留下一个“工具调用”而删掉其结果。
当前需续接的 provider opaque 数据保留原语义；不从 UI 文本反推完整模型历史。

### 3.1 附件物化边界

消息档案始终保留原始 `file_reference / image` 与 ArtifactRef。Composer 在估算与规范化 hash **之前**生成真实模型内容，同时将附件加入 ContextView.material_refs：

- UTF-8 的 `text/*`、JSON、XML、YAML 读取至 context policy 的 `offload_bytes` 上限，转换为带来源、摘录和明确 `truncated` 字段的文本材料。截断不删除原对象；继续读取需走已有产物读取工具。多字节字符恰好位于摘录边界时只保留完整字符。
- 图片仅在该 profile revision 的 `vision` 已验证时，转换为 LangChain 标准 `{type:"image", base64, mime_type}`；真实 provider adapter 负责映射其协议。支持的媒体类型为 PNG/JPEG/WebP/GIF，单图读取上限 10 MiB。无已验证能力时返回 `vision_unverified`，不会把引用 JSON 回显当作理解图片。
- 其他二进制格式（包括 PDF、DOCX）当前没有内容提取器，返回明确的 `unsupported_attachment_type`；上传与可下载不代表模型已读取内容。

图片的完整物化请求进入保守估算，因此大图可能先触发硬窗口错误；没有校准过的视觉 tokenizer 时不得虚报精确图像 token。文本摘要只记录图像来源及未做视觉总结的说明，仍将原图像消息保留在活跃输入，不以文本摘要代替图像事实。图像语义能力和效果需按 03/13 单独验收。

## 4. 输入预算与裁剪策略

从 ModelProfile 取得已规范化的联合窗口 `W`、独立输入上限 `I`、计划输出 `O` 与余量 `S`：`B = min(I, W - O) - S`。
未提供的独立限制忽略；若输入限制已排除输出，不能再次扣除输出。推理 token 的归属依 profile 处理。
系统指令、工具 JSON schema、图片、Skill、历史和协议包装均计入输入预算；不能只统计聊天正文。
无精确 tokenizer 时使用版本化保守估算并留余量，实际 usage 只用于校准，不倒填为“当时精确值”。
初始软阈值可设为 `0.75B`，压缩目标 `0.50B—0.60B`；参数进入 context policy，而不是散落在 middleware 常量中。
达到软阈值先卸载大输出，再压缩旧回合；接近硬上限再减少可选检索材料与工具目录。
硬约束、最新用户修正、待处理工具配对不能为了凑 token 静默删除。
若必需内容本身超限，返回明确不可组装原因；由 runtime 形成可解释结果，不能发送已知超限请求。
更换到小窗口模型时重新执行整个组装过程，不能沿用原 profile 的预算结论。

## 5. 压缩提交流水线

1. 固定候选消息区间和 `source_revision`，把原始大输出保存为共享 ArtifactRef。
2. 用可读取的产物引用及必要摘录代替巨大结果；产物仍受原访问权限约束。
3. 选择完整旧回合；排除最新修正、未决交互和仍需续接的协议片段。
4. 通过 ModelGateway 调用摘要模型，继承出站限制并计入根任务预算。
5. 校验摘要字段、来源可解析性、硬约束保留和工具配对合法性。
6. 调用存储模块保存不可变摘要，再通过版本比较更新 active pointer。
7. 重新构造 ContextView，成功后发布上下文变化通知，公开事件归 [10](10-api-events.md)。

如果版本比较失败，丢弃指针更新并按新 revision 重建；旧候选可以留作未生效记录，但不能覆盖新消息。
运行中的新输入如何准入由 [06](06-scheduler.md) 定义；本模块仅消费明确可见输入版本。
摘要调用失败保留原有生效版本，原始档案不变；仍有预算时可以继续，否则停止本轮模型调用。
本模块只启用一个摘要引擎，可包装 LangChain 能力；不得同时启用另一套自动摘要 middleware。
卸载与摘要共享同一调度入口，避免多个 hook 在同一回合重复处理相同材料。
生产摘要模型按当前 run 的不可变快照 `agent_spec.model_policy.summary_profile_ref` 选择；未配置时使用快照的主模型 profile。选择结果只属于本次调用，不修改跨 run 共用实例的默认配置。确定性演示摘要仅验证结构、约束保留及版本关系，不代表真实智能摘要效果。
激活摘要的仓储事务同时重验 pin revision 和原 context revision，再切换指针；新固定内容不能被旧候选覆盖。压缩开始、失败和提交的内部观测记录分别为 `context.compaction_started / context.compaction_failed / context.compacted`，包含 run/trace 与耗时；公开事件由 10 所有者投影。

## 6. P1 长期记忆实现

先交付可审阅记录和关键词检索，向量索引在效果评估需要时再引入。
写入按“提议 → 策略判定 → 所需确认 → 持久化 → 更新索引”处理，不把所有消息自动记忆化。
检索必须先进行身份、工作区、作用域和授权过滤，再计算关键词或语义相关性。
每条被选中记忆带来源和时效；过期、被删除和未接受的提议不得注入新 ContextView。
纠正生成新 revision 并使旧索引失效；删除同步处理主记录可见性、索引和缓存，历史档案保留按存储策略处理。
用户固定事实属于受预算保护的输入，不具有无限窗口特权；超限时返回具体冲突。
归档删除、备份保留与对象垃圾回收由 [02](02-storage.md) 管理，本模块只提供引用与失效通知。

## 7. 交付物与完成依据

### 实施补充：用户固定内容

用户可将明确文本固定到当前 branch：`ContextPin = {id, text, source_ref}`，集合为 `{revision, items}`，存储 key 为 branch_id。固定接口要求集合的 expected revision，且不改变 run 输入命令或权限。Composer 在每次安全模型边界读取集合并作为受保护硬约束；ContextView 记录集合 revision 与实际 pin_refs。压缩提交除原 source revision 外重验 pin revision，变化则保留候选但拒绝切换旧摘要指针。固定内容本身超出窗口时报告预算冲突，不能静默截断。创建会话分支时继承源 branch 的固定集合副本，之后分别编辑；分支发布和源检查归 06 所有。

交付 `composer`、`budget`、`compaction`、`memory` 模块与一份可配置 context policy。
提供 ContextView 查看器所需读模型：来源清单、类别预算、压缩点与产物引用；不在本模块实现页面。
提供长中文会话、大工具输出、冲突记忆、未决工具配对的固定输入样例。
验收执行 [13 · C01—C03](13-verification.md)，保留实际输入快照、摘要差异和版本冲突处理记录。
完成证据应区分“预算未超限”和“约束语义仍保留”；仅缩短了文本不代表压缩有效。

## 8. 设计来源

- [原系统设计 §6](../通用Agent-Harness系统设计文档.md#s6)：上下文对象、预算、压缩和长期记忆原则。
- [LangChain / LangGraph 调研](../research/langchain-langgraph.md)：第 4、5、10、15 节的单一摘要入口及恢复边界。
- [DeepSeek Harness 调研](../research/deepseek-harness.md)：可追溯会话、压缩范围和原始资料保留的参照。
