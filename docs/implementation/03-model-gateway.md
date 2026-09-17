# 03 · 模型接入与能力验证实现指南

[实现导航](README.md) · [共享契约](01-contracts.md) · 先决：[存储](02-storage.md)、[平台策略与凭据](12-platform.md)

本文是 `packages/model_gateway/` 的实现依据，定义模型 profile 和适配接口。所有类型为拟议项目契约，框架调用参数以最终锁定版本为准；本指南不表示接入已通过验证。

## 1. 职责与交付边界

负责把实际 provider 的模型接口变成 `create_agent` 可使用的模型对象，处理能力验证、协议差异、完整响应校验、调用尝试与 usage。
不选择历史消息，不推进 Agent 循环，不执行工具，不管理 worker 队列。
上下文输入来自 [04](04-context.md)，挂接位置由 [05](05-runtime.md) 定义，预算预留与并发许可使用 [06](06-scheduler.md) 的接口。
鉴权、密钥解析、端点出站判定交给 [12](12-platform.md)；对浏览器的配置 API 由 [10](10-api-events.md) 提供。

首期交付两种云端适配路线和一种本地路线；具体模型型号由验证结果决定，不在业务代码写死。
优先包装供应商专用 LangChain 集成；兼容端点必须声明协议模式，不能仅凭路径相似宣称完整兼容。

## 2. 本模块唯一维护的数据契约

### 2.1 ModelProfile

| 字段 | 含义与约束 |
|---|---|
| `profile_id / revision` | 不可变能力快照标识；run 锁定 revision |
| `provider_id / adapter_id / adapter_version` | 供应商配置与实际适配实现版本 |
| `endpoint_ref / credential_ref` | 受控配置引用；不包含地址鉴权参数或密钥明文 |
| `model_id / api_mode` | 实际型号和 API 模式，不能只存一个展示名 |
| `capabilities` | 文本、工具调用、并行工具、结构化输出、视觉、usage 等能力项 |
| `limits` | 联合窗口、独立输入/输出上限、推理 token 计入方式及来源 |
| `token_estimator` | 精确 tokenizer 引用或估算算法版本、误差余量 |
| `verification_ref` | 契约测试版本、环境、日期、脱敏证据引用 |
| `continuation_requirements` | 下轮必须保留的 provider 字段与迁移限制 |

每项能力保存 `verified / unverified / unsupported` 及证据引用。这是模型能力状态，不是共享 `RunStatus`。
`verified` 绑定 adapter、endpoint、model 与 API 模式组合；其中任一项改变后重新验证受影响能力。
未知窗口和未知价格保留空值，不能以零或未经核实的网上默认值填充。
配置修改生成新 revision；活跃 run 不原地读取“最新” profile。

### 2.2 ProviderAdapter 与 ModelGateway

```text
ProviderAdapter.describe(config_ref) -> ModelProfile
ProviderAdapter.build_model(profile, credential_accessor) -> BaseChatModel
ProviderAdapter.estimate_tokens(request) -> TokenEstimate
ProviderAdapter.validate_response(assembled_response) -> ValidatedAssistantTurn
ModelGateway.resolve(profile_ref, execution_context) -> GatewayModelHandle
ModelGateway.verify(profile_ref, suite_ref, diagnostic_context) -> verification_ref
```

`GatewayModelHandle` 是可接入 LangChain 的模型包装，必须覆盖实际使用的异步调用、流式调用与 `bind_tools`；绑定后仍保留网关治理，不得返回裸 SDK 对象。
共享 `ExecutionContext` 从 [01](01-contracts.md) 导入。模型对象和凭据访问器是运行时资源，不进入 checkpoint。
`TokenEstimate` 携带总量、按内容类别拆分的估算、算法版本和误差属性；预算公式归 [04](04-context.md)。
`ValidatedAssistantTurn` 保存完整 LangChain assistant message、完整 tool calls、usage 与必要 provider metadata；业务 ID 引用共享契约，供应商字段转换归本 adapter。
该对象通过校验不等于已持久化；下一工具步骤前的持久屏障由 [05](05-runtime.md) 落实。

`verify` 使用 01 的 DiagnosticContext，只运行固定 fixture 和有界模型请求；多轮工具协议探针注入预设结果，不执行真实工具。真实模型尝试仍经本网关治理与 06 的预算/全局并发许可，归诊断账户结算。报告区分协议探针与真实工具 E2E；后者使用普通测试 run，不能用诊断身份绕过 07。

## 3. 按顺序实现

### 3.1 建立配置到模型对象的最短链路

1. 定义 profile 仓储调用，沿用 [02](02-storage.md) 的版本化记录机制。
2. 为首个 provider 实现 adapter 工厂；对缺包、错误 API 模式、未配置模型返回可定位配置错误。
3. 由平台接口判定出站及访问凭据，再创建 client；主模型、摘要模型均走同一入口。
4. 封装 `bind_tools`，验证工具 schema 可表达性，并保留原 schema hash 供 ContextView 记录。
5. 用显式模型配置跑一个完整只读工具回合后，再接入 runtime；连通性成功单独记录。

避免在全局 module import 时访问密钥或发网络请求。按进程管理 client 生命周期，关闭时释放连接池。
本地模型地址也经过端点策略；“本地”不代表绕过配置校验或允许任意 URL。

### 3.2 保留消息语义

用 LangChain 消息和内容块保留文本、图片、tool calls 与 tool results；禁止统一转换成纯字符串。
provider 原始字段分为协议续接所需、可展示、仅内部保存三类；按白名单保存，诊断输出先脱敏。
签名块、缓存字段和不透明续接数据不向其他 provider 强行转码，也不作为用户可查看推理过程的承诺。
工具结果必须携带原 tool-call ID；跨轮历史校验检查调用和结果一一配对。
provider 特有格式转换发生在 adapter 内，不泄漏到 Scheduler、Web 或 ToolGateway。

### 3.3 流式聚合与完成判定

1. 每次实际网络尝试关联共享 `model_attempt_id`，内部缓冲按该标识隔离。
2. 文本增量可交事件适配器用于临时展示；工具参数增量只进入缓冲器。
3. 接收结束标识后组合完整响应，校验 JSON、工具名、参数 schema、调用 ID 与消息协议。
4. 只有校验后的完整响应才能返回给 Agent 图；断流、截断 JSON 或缺少必需字段均不得透传为可执行调用。
5. 转换 usage；供应商未返回时标记估算或未知，并在预留预算结算时保留这种区别。

结束标识不等于业务成功，网络 HTTP 200 也不等于协议正确。
同一回合的多个 tool calls 可以一起返回；是否允许并行由 profile、runtime 与工具资源约束共同决定。
事件字段和公开错误结构只引用 [10](10-api-events.md)，不在本模块再造 Web 流协议。

### 3.4 能力降级与模型切换

| 不满足项 | 网关行为 |
|---|---|
| 原生工具调用未验证 | 仅开放文本路径；禁止静默从文本推断并执行工具 |
| 不支持并行工具 | 关闭该请求能力，使用受支持的单工具回合 |
| 结构化输出不可用 | 按明确配置启用有界 schema 修复；修复仍计入预算 |
| 图片不可用 | 返回能力不匹配，由上层选择获准的转换工具或其他 profile |
| 上下文限制变小 | 要求 Context Service 重建 ContextView，不复用超限输入 |
| 续接状态不可迁移 | 收束当前回合或重建标准历史后再切换；无法满足则停止切换 |

选择候选模型先满足出站、能力、预算和上下文条件，再使用价格或延迟偏好。
模型切换只发生在完整回合边界；已有未完成工具配对时禁止切换。
切换产生新 profile 引用和 ContextView，保留可回查的选择原因。

## 4. 重试、限额与错误处理

Gateway 是一次模型调用总重试预算的唯一所有者；SDK 内置重试关闭或纳入同一计数，middleware 与 worker 不再额外套模型重试。
认证失败、无效模型、无效参数、能力不匹配直接返回；限流与可恢复网络故障按有界退避和 jitter 重试。
尝试开始前取得 Scheduler 的调用许可和预算预留；取消、deadline 或无预算时不再发请求。
每次重试都是新 attempt，不把前次临时文本拼进后次回答，也不把前次未完成工具调用留在历史中。
对于发送成功但 usage 丢失的请求，费用不宣称为零；保留估算与对账信息。
收到上下文溢出时携带实际限制证据，最多走配置允许的重建路径，不无限缩减后重试。
错误日志记录 adapter、profile revision、请求/尝试关联和可重试属性；不记录鉴权头或完整用户内容。
runtime 根据共享错误与结果契约处置 run；Gateway 不直接写 `RunStatus`。

## 5. 工程交付物与完成依据

交付 `profiles`、`registry`、`providers`、`stream_assembler`、`usage` 模块，以及经过版本锁定的依赖清单。
提供脱敏模型能力报告、原始协议样例 fixture、工具 schema 兼容说明和已知限制列表。
所有 provider 共用一套契约测试入口，provider 差异通过 fixture 和能力条件表达，避免复制测试逻辑。
保留每次兼容测试的 model、endpoint 类别、adapter 版本、API 模式和环境；“已验证”必须可追溯到报告。
验收执行 [13 · M01—M03](13-verification.md)，本篇不另列通过标准。
同时向 runtime 提供“完整响应返回前后”的可控故障注入点，供跨模块恢复用例使用。

## 6. 简化模型接入（2026-09-17 修订）

模型配置的产品入口由提供方目录驱动，接收提供方、临时 API Key 与模型名称；底层 ModelProfile 保持既有版本契约。内置提供方的地址、adapter/API 模式、官方模型预设与来源由服务端维护，客户端不能修改内置提供方的地址；自定义 OpenAI 兼容与 Ollama 服务允许显式地址。可发现的模型通过有界目录请求获取，不提供目录 API 的服务返回有来源的预设，不能把预设标为实际连接成功。

测试与保存使用独立 DiagnosticContext、固定协议探针和限额，不执行真实工具。保存前验证文本与多轮工具协议，通过后才能将这些能力标为 verified；在相同总时限与最多四次调用内追加可选流式检查，只有通过才启用该能力。未通过专项探针的其它能力保持未验证。自动填充的保守上下文/输出限制标为应用默认值，不宣称供应商最大窗口或价格。必要协议检查失败不改当前使用模型。

实际任务依据该 profile 的流式能力证据选择流式或完整响应；流式尚未验证时使用普通调用，不因 Worker 的全局流式偏好把已验证的普通模型调用变成必然失败。验证必须覆盖保存后通过真实 Worker 执行后续任务，而不只检查配置写入与 ContextView。

`create_agent` 在 `bind_tools` 阶段传入的 Host 预算参数也由 Gateway wrapper 消费并保留，不能绑定到裸 SDK 后随请求透传。供应商只收到相应的输出 token 限额参数；该边界由保存后真实 Worker 的完整工具回合验证。

新建/编辑先读取当前 revision，网络检查结束后在短事务内再次 CAS，再发布 profile、精确端点授权与默认 Agent 引用；不持事务等待网络。每个已保存 revision 的凭据引用保持固定，轮换 Key 不破坏旧任务。浏览器交互归 [11](11-web.md)，敏感传输归 [10](10-api-events.md)，系统凭据与端点授权归 [12](12-platform.md)。

## 7. 设计来源

- [原系统设计 §5](../通用Agent-Harness系统设计文档.md#s5)：模型兼容、路由、流式聚合和预算边界。
- [LangChain / LangGraph 调研](../research/langchain-langgraph.md)：第 4、8、14、15 节的复用与契约验证建议。
- 实施时核对调研附件引用的官方 adapter 文档并锁定版本；本次拆分沿用原研究，不增加新的兼容性结论。
