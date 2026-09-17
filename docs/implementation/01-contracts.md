# 01 · 公共契约与模块边界

[实现入口](README.md) · 适用基线：系统设计 1.0 · 状态：实现规范，尚无运行代码

本文件是跨模块术语、标识、基础信封与不变量的唯一规范来源。模块专有字段在各自文档定义；数据库映射见 [02](02-storage.md)，HTTP 映射见 [10](10-api-events.md)。本文的类型描述用于生成后续 Pydantic/schema/TypeScript 类型，不冒充已存在的 SDK API。

## 1. 实现位置与依赖方向

拟议目录 `packages/core/contracts/`：`identity.py`、`execution.py`、`operations.py`、`artifacts.py`、`events.py`、`errors.py`。只依赖语言标准库与统一校验库，不导入 LangChain、数据库、FastAPI 或前端模块。

调用者依赖这些契约及业务端口，实现通过组合根注入。跨进程使用可序列化 DTO；连接、凭据访问器、取消 token 等能力通过本进程服务容器提供，不伪装成可序列化字段。

## 2. 标识与归属

| 标识 | 含义与生成规则 |
|---|---|
| `owner_id` | 已认证主体；P0 单用户也保留，不接受模型指定 |
| `workspace_id` | 授权资源容器；路径另存，ID 不含真实路径 |
| `session_id` | 用户会话，不等同 graph thread 或 MCP session |
| `branch_id` | 独立会话历史；P0 创建默认 branch，P1 开放分支 UI |
| `graph_thread_key` | branch 绑定的持久执行键；重试/恢复沿用，child 使用独立键 |
| `run_id` | 一次逻辑任务，从提交至终态；多次调度仍是同一 run |
| `worker_attempt_id` | 一次 worker 领取及执行尝试，用于租约与故障审计 |
| `model_attempt_id` | 单次模型请求尝试；不能与 worker attempt 混用 |
| `message_id` | 完整消息的内部稳定 ID；模型回合保存后不可更换 |
| `tool_call_id` | 在对应 assistant 消息中唯一的调用标识；保留供应商映射 |
| `operation_id` | 执行账本逻辑操作标识；恢复不能重新生成 |
| `interaction_id` | 面向用户的等待对象，关联具体 interrupt 与参数版本 |
| `wait_id` | Scheduler 内部等待对象，如等待 child；不显示为审批 |
| `artifact_id` | 受权限保护的产物逻辑标识，与文件路径及内容 hash 分离 |

除具有协议要求的外部 ID 外，内部 ID 使用不可预测的 UUID 或等效标识，生成策略全项目一致。所有时间持久化为 UTC RFC3339，UI 按用户时区显示。期限判断由服务端时钟完成；进程内耗时使用单调时钟。

外部 MCP request/session/remote handle 只在 [09](09-mcp.md) 内映射，不能用作以上业务主键。

## 3. 公共状态枚举

`RunStatus` 的完整取值只在这里定义：

```text
queued, running, waiting_user, waiting_children,
recovering, needs_review, cancelling,
completed, failed, cancelled
```

`completed/failed/cancelled` 是终态，其余为非终态。状态转换、CAS 条件、branch 所有权及预算由 [06 Scheduler](06-scheduler.md) 唯一定义。Graph interrupt、工具 `unknown`、MCP 连接失败不是新的 RunStatus。

终态 run 不直接改回 queued；显式重新执行创建新 run。非终态恢复保留 run_id，并记录新的 worker_attempt_id。多个并行 interrupt 以各自 ID 路由，不能使用“最近一个审批”作为恢复目标。

## 4. ExecutionContext 与 ConfigSnapshot

`ExecutionContext` 是 Host 建立的只读执行身份：

| 字段 | 类型 / 语义 |
|---|---|
| `owner_id/workspace_id/session_id/branch_id/run_id` | 上述标识 |
| `root_run_id/parent_run_id` | 根任务与父任务；根的 parent 为空 |
| `worker_attempt_id/fencing_token` | 当前合法执行者；重新领取时变化 |
| `graph_thread_key/input_revision` | 本次 graph 输入与恢复坐标 |
| `config_snapshot_id` | 固定配置引用 |
| `deadline_at/trace_id` | 全任务期限与观测关联 |

`ConfigSnapshot` 记录 AgentSpec revision、模型路由/profile revision、context policy revision、可用工具 schema hash、Skill/plugin hash、MCP 配置 revision、policy revision、预算策略和数据出站规则引用。具体值结构分别由模块所有者定义，本文件不重复。

快照用于复现行为，**不阻止撤销授权**：执行时以快照的许可上界与当前更严格限制取交集；扩权需要新授权，不能因配置热更新而静默授予。密钥值不进入 context/snapshot/checkpoint；凭据引用的结构归 [12](12-platform.md)。

预算配置是不可变上限，消费/预留是 Scheduler 的事务性状态。worker、模型网关、工具只能调用预算服务，不能各自维护互相不一致的根任务余额。

设置页诊断使用独立的 `DiagnosticContext`，字段为 `diagnostic_id, owner_id, workspace_id?, config_snapshot_id, budget_account_id, deadline_at, trace_id`。Host 基于已认证请求生成它，不伪造 run、branch 或 worker attempt。workspace 为空时仅允许账户级配置探针，不得读取工作区资源。

`AccessContext = ExecutionContext | DiagnosticContext` 只供明确接受两种身份的模型验证、配置发现及策略接口使用；RuntimeAdapter 和工具执行仍只接受 ExecutionContext。诊断范围由 12 限制，预算由 06 管理；实际示例工具调用须另行提交可见的测试 run 并走 07 账本。

## 5. OperationKey 与 ApprovalBinding

```text
OperationKey = (run_id, message_id, tool_call_id)
ApprovalBinding = (
  operation_id, tool_revision, args_hash,
  resource_fingerprint, policy_revision, expires_at
)
```

数据库为 OperationKey 建唯一约束，首次登记生成 operation_id。`args_hash` 是同一操作参数不漂移的校验值，不是操作去重主键；用户有意执行两次相同命令应形成两个操作。

参数先通过工具 schema 校验，再以项目统一规范化器计算 hash：UTF-8、键排序、固定紧凑 JSON 编码、拒绝 NaN/Infinity；涉及精确金额/大数的字段按 schema 使用字符串，不在各适配器里自由转换。资源 fingerprint 由执行器基于实际目标与预期文件版本生成。

批准与具体绑定关联；参数、工具版本、资源或授权变化后不能复用旧批准。ApprovalBinding 是授权证据引用，不表示操作已执行成功。[07](07-tools.md) 负责执行账本算法，[12](12-platform.md) 负责判断是否获得有效授权。

## 6. RuntimeOutcome

RuntimeAdapter 返回 DTO，不直接修改 run 的最终状态：

```text
kind: final | waiting | error | cancel_ack
run_id, input_revision, checkpoint_ref
result_ref?: ArtifactRef
interrupt_refs?: list[InterruptRef]
error?: ErrorEnvelope
```

`InterruptRef` 包含框架 interrupt ID、checkpoint 引用，以及 `user/children/review` 等 Host 交互分类；字段映射由 [05 Runtime](05-runtime.md) 维护。`waiting` 不等于失败，`cancel_ack` 不证明外部操作已撤销。

`CheckpointRef = (graph_thread_key, checkpoint_ns, checkpoint_id, runtime_revision)`。它是持久位置引用，不包含图全部内容，也不自动赋予读取其他 branch 的权限。

Worker 只有在核验对应 run/input_revision、持久化完成和副作用/子任务状态后，才能调用 Scheduler 的结算入口。不能只看 graph thread 的最新状态就结算旧 run。

## 7. ArtifactRef

```text
artifact_id, content_hash, mime_type, size_bytes,
owner_id, workspace_id, provenance_ref
```

ArtifactRef 不含任意宿主文件路径。获取内容必须通过 ArtifactStore 与访问检查；内容 hash 相同不意味着不同 owner 可以互读。未完成临时文件不能生成可对外引用的 ArtifactRef。存储、持久化屏障与 GC 见 [02](02-storage.md)。

## 8. EventEnvelope 与 ErrorEnvelope

```text
EventEnvelope:
  schema_version: 1
  event_id, run_id, type, timestamp
  durability: durable | ephemeral
  seq: integer | null
  data: event-specific payload

ErrorEnvelope:
  code, message, retryable, correlation_id
  details_ref?: ArtifactRef
```

durable 事件有 run 内单调 seq；ephemeral 不占用耐久游标，seq 为 null。事件类型、具体 payload、SSE 帧与重连只在 [10](10-api-events.md) 定义。日志 trace 不自动等于用户可见 event。

`retryable` 表示错误类别允许调用者进一步判断，不是对副作用安全性的承诺。是否重试仍由模型网关或工具执行策略决定。面向用户的 message 应可读，敏感诊断放受控 details_ref。

## 9. 契约所有权清单

| 契约 / 决策 | 唯一定义位置 |
|---|---|
| 以上公共 DTO、ID、RunStatus | 本文件 |
| 数据库 schema、repository、checkpoint 协调与对象存储 | [02](02-storage.md) |
| ModelProfile、ProviderAdapter、模型错误/重试策略 | [03](03-model-gateway.md) |
| ContextView、摘要与长期记忆结构 | [04](04-context.md) |
| AgentSpec、RuntimeAdapter、middleware 与 interrupt 映射 | [05](05-runtime.md) |
| 状态转换、任务领取、预算、父子关系与唤醒 | [06](06-scheduler.md) |
| ToolSpec、ToolResult、操作账本状态与执行器 | [07](07-tools.md) |
| SkillRecord、PluginManifest 与注册生命周期 | [08](08-skills-plugins.md) |
| MCP profile、McpGateway 与连接资源 | [09](09-mcp.md) |
| HTTP 契约、事件类型与 SSE | [10](10-api-events.md) |
| 前端视图模型、页面与交互 | [11](11-web.md) |
| 配置优先级、PolicyDecision/ExecutionGrant、鉴权、发行 | [12](12-platform.md) |
| 测试用例定义、PoC、发布门槛 | [13](13-verification.md) |

修改已定义契约，先在所有者文档及 schema 中变更，再更新调用者；不在模块文档复制一份“局部版本”。跨模块冲突以所有者定义为准，不能靠各模块默默兼容不同含义。

## 10. 交付物

生成 core schemas、带版本的序列化样本及契约变更规则；其他模块通过依赖导入这些类型。契约验证与文档一致性统一交由 [13 的 CT01](13-verification.md#ct01)。

设计来源：[原设计 §3](../通用Agent-Harness系统设计文档.md#s3)、[§7～8](../通用Agent-Harness系统设计文档.md#s7)、[§13～14](../通用Agent-Harness系统设计文档.md#s13)。
