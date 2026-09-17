# 05 · Agent 配置与运行内核实现指南

[实现导航](README.md) · [共享契约](01-contracts.md) · 先决：[存储](02-storage.md)、[模型](03-model-gateway.md)、[上下文](04-context.md)

本文是 `packages/runtime_langchain/` 的实现依据，唯一维护 AgentSpec、RuntimeAdapter、InteractionRecord 和 `create_agent` 挂接规则。本文给出逻辑接口，不假设尚未锁定的框架版本支持某一具体方法签名。

## 1. 职责与必须保持的边界

默认 `LangChainAgentRuntime` 用 `create_agent` 持有唯一的模型—工具循环，负责组装、调用、暂停和恢复图。
必要时使用外层 StateGraph 做预处理与结果校验，但外层不得再写模型—工具循环。
模型路由、上下文与工具分别只调用 [03](03-model-gateway.md)、[04](04-context.md)、[07](07-tools.md) 的统一入口。
runtime 不领队列、不续租、不持有 branch 调度权、不结算 run；这些由 [06](06-scheduler.md) 负责。
runtime 返回共享 `RuntimeOutcome`，由 Worker 在持久性和资源条件满足后提交最终业务状态。
后续可选 DeepAgentRuntime 必须通过同一公共契约验收；不同 runtime 的 checkpoint 不承诺互相恢复。

## 2. 本模块唯一维护的数据契约

### 2.1 AgentSpec

| 字段 | 实现要求 |
|---|---|
| `id / revision` | Agent 模板与不可变版本 |
| `runtime` | 受支持的 runtime 实现标识 |
| `system_prompt_ref` | 版本化提示内容引用 |
| `model_policy` | 可解析到 [03](03-model-gateway.md) profile 的模型选择策略 |
| `tools / skills / mcp_servers` | 能力引用，解析为本次 run 的锁定清单 |
| `policy / context_policy` | 平台授权策略引用和上下文策略引用 |
| `budget` | 预算配置引用或限额；字段与权威计数遵循 [06](06-scheduler.md) |

AgentSpec 是配置，不直接持有模型 client、运行线程或工作区锁。
保存时验证引用可解析，启动时验证有效配置和能力交集；模板修改不改变正在运行的快照。
快照包含所用提示、工具 schema、Skill 内容及 MCP 目录的版本引用，序列化与存储遵循 [02](02-storage.md)。
AgentSpec 不能扩大用户或工作区授权；effective policy 由平台服务提供。

### 2.2 RuntimeAdapter

```text
start(execution_context, agent_snapshot_ref, input_ref) -> RuntimeOutcome
resume(execution_context, checkpoint_ref, resume_payload_ref) -> RuntimeOutcome
request_cancel(execution_context) -> acknowledgement
get_checkpoint(execution_context) -> checkpoint_ref
inspect_interrupts(execution_context, checkpoint_ref) -> interrupt_refs
stream_events(execution_context) -> async stream of internal runtime records
```

`ExecutionContext`、`RuntimeOutcome`、ID 与 checkpoint 引用采用 [01](01-contracts.md)；不得在适配器内复制 RunStatus 枚举。
事件流返回内部运行事实；映射到 EventEnvelope、耐久事件和 SSE 的责任归 [10](10-api-events.md)。
`request_cancel` 仅请求协作停止，acknowledgement 不表示外部工具已撤销或 run 已取消。
`start/resume` 应在一个图调用结束、暂停或出现明确故障后返回；浏览器连接生命周期不影响调用。

### 2.3 InteractionRecord

这是面向用户交互的唯一 DTO；[10](10-api-events.md) 只做脱敏投影，内部 child wait 和副作用核对不伪装成用户审批。

| 字段 | 实现要求 |
|---|---|
| `interaction_id / run_id / input_revision` | 共享稳定标识与交互所对应的输入版本 |
| `interrupt_id / checkpoint_ref` | 可恢复 interrupt 与已持久 checkpoint；pending_binding 时允许为空 |
| `kind` | `approval / user_input / mcp_elicitation`；P0 仅前两类，后者 P1 启用 |
| `prompt / response_schema / payload_ref` | 用户问题、严格答复 schema、受控审阅材料引用；不包含凭据 |
| `revision / expires_at` | 乐观并发版本和可选到期时间；状态/内容变化递增 revision |
| `binding_ref` | 可选 ApprovalBinding 引用；approval 必须提供，普通补充信息不得冒充批准 |
| `status` | `pending_binding / open / resolved / expired / cancelled`，是交互状态而非 RunStatus |

创建为 `pending_binding`；准备阶段使用产生方的稳定逻辑请求键去重，不能仅依赖尚未生成的 interrupt ID。
只有 interrupt/checkpoint 已持久并经 Worker 绑定、run 已进入对应等待态，才允许转为 `open` 并发布 interaction.required。
未绑定记录不向用户提供可答复入口；内部诊断可以显示准备进度，但 respond 必须拒绝该状态。
`open → resolved` 要求当前 revision、未到期、回复符合 response_schema，并在需要批准时验证 binding_ref；拒绝也属于已答复，不等于授权。
答复记录经 [06](06-scheduler.md) 的 respond 服务原子保存和单次消费；是否具备恢复条件由其协调，runtime 按明确 interrupt ID 读取回复集合。
`pending_binding/open → expired/cancelled` 分别用于超时或等待撤销；交互终态不可再次接受新答复，幂等重试返回原回执。
绑定中断或重启后由恢复协调器对照指定 checkpoint 补齐绑定；过时或无法关联的记录保持不可答复并进入诊断，不能绑定到“最新 checkpoint”冒充原等待。
用户交互状态不自行推进 run；答复批准不直接创建执行成功结论，恢复后的当前策略与资源重验仍归 [07](07-tools.md) 和 [12](12-platform.md)。

恢复输入在本模块统一为 `InterruptResolution = {interrupt_id, outcome, response_ref?, reason}`，outcome 为 `answered / expired / cancelled`。到期扫描经 06 将 expired 状态及唯一超时 resolution 一起持久化；它是屏障的已满足项，不要求补造用户答复。approval 超时返回无授权的拒绝结果，user_input 超时返回明确缺少输入，P1 elicitation 超时映射协议 cancel 并记录 Host 到期原因。runtime 不能从这些结果推导批准或业务成功；重新提问必须形成新的逻辑交互并受轮次/期限限制。

run 取消由 06 的取消屏障统一处理，不能因为产生 cancelled resolution 又把 run 排回执行。pending_binding 到期时保存到期意图，待核实原 checkpoint 后绑定超时 resolution；不存在对应 checkpoint 的孤立记录只做回收诊断，不猜测恢复目标。

## 3. 可序列化图状态与进程资源

图状态保存工作消息、ContextView 引用、计划、步骤计数、摘要/产物引用、交互引用和 child run 引用。
与恢复有关的配置版本、来源清单和 runtime 状态版本也必须可追溯。
身份、授权与预算快照只能作为辅助引用，执行时仍由权威服务校验；不根据旧 checkpoint 自行授予权限。
数据库连接、HTTP/MCP client、密钥明文、锁对象、进程句柄与取消 token 不进入图状态。
恢复时用 ExecutionContext 中的身份与快照引用向进程服务容器取得访问器，再恢复可序列化状态。
并行节点更新共享字段时定义明确 reducer，禁止靠“最后写入胜出”合并消息或产物。
图状态 schema 变化遵循 [02](02-storage.md) 的兼容与迁移策略，不在同一 thread 中直接混用不兼容版本。

## 4. 按顺序建立唯一循环

### 4.1 最短调用链

1. 校验 Worker 传入的 ExecutionContext 与不可变 Agent 快照，不从请求参数临时拼装权限。
2. 向 ModelGateway 取得受治理模型句柄，向 ToolGateway 取得 LangChain 工具外壳。
3. 实例化 `create_agent`，挂接 context schema、持久 saver、状态扩展和下述 middleware。
4. 使用 Scheduler 提供的稳定 thread/checkpoint 关联启动，图调用指定 `durability="sync"`。
5. 转换运行流中的事实，返回共享 RuntimeOutcome；不直接更新业务 run 终态。

LangChain 工具外壳只描述 schema 并转交 ToolGateway；MCP 原始工具和 Skill 脚本不得作为旁路执行器直接挂载。
真实包 API 与 middleware 顺序在依赖锁定后通过小型探针验证，记录在兼容报告，避免只照伪代码认定可用。

### 4.2 Middleware 职责表

| 挂接点 | 唯一责任及结果 |
|---|---|
| Agent 开始前 | 加载快照、检查输入版本、关联 trace；只做可重放准备 |
| Model 调用前 | 取消/限额检查，调用 Context Service 生成 ContextView |
| Model 包装层 | 调用 ModelGateway；路由与重试规则不在此再次实现 |
| Model 调用后 | 接收完整响应、提取使用量事实、更新循环检测依据 |
| Tool 包装层 | 传递共享 OperationKey 与上下文到 ToolGateway，接收执行/等待结果 |
| Agent 结束后 | 校验候选最终回复、产物引用与记忆提议，不提交 run 完成 |

框架并行工具仍逐一进入 ToolGateway；冲突写资源串行化由工具模块负责。
使用框架 hook 的真实执行顺序作为集成检查项，不能靠表格顺序假设并行、异常和恢复路径都一样。
上下文摘要只由 Context Service 驱动；不能额外启用 Deep Agents 或第二个 SummarizationMiddleware 自动压缩。

## 5. 模型响应到工具步骤的持久屏障

P0 必须使用持久 saver 与 `durability="sync"`，使包含完整模型响应的 checkpoint 在下一工具步骤前完成。
Runtime 在完整响应校验前不得产生可执行工具请求；临时 token 或参数 delta 只供展示/缓冲。
完整响应进入 checkpoint 后，恢复应沿原 tool-call ID 构造共享 OperationKey，不能重新生成 ID 逃过账本去重。
工具 ledger 已完成而图 checkpoint 未更新时，恢复仍调用 ToolGateway，由其返回已持久结果；算法只见 [07](07-tools.md)。
saver 出错时停止推进，禁止“先继续工具、稍后补存”。
`sync` 只定义 checkpoint 的时机，不保证业务表与 checkpoint 自动处于同一数据库事务。
如未来改用异步持久化，必须先提供等效的持久模型输出 journal 与工具前置屏障，再做故障验证。

## 6. Interrupt 与恢复挂接

1. 从 ToolGateway 或其他交互服务取得已保存的等待对象引用，保持规范化参数和稳定关联。
2. 以可序列化数据调用框架 interrupt；暂停前不执行尚待授权的操作。
3. 发现图暂停后返回共享 RuntimeOutcome，由 Worker 处理等待态和执行槽。
4. 恢复时校验 checkpoint 与有效回复的对应关系，按 interrupt ID 分配答复。
5. 从同一 thread 恢复；等待节点再次进入时读取原等待对象并重新校验，不创建重复审批。

InterruptRef 在此定义映射：`interrupt_id` 是 Host 逻辑中断 ID，持久恢复点对应 `checkpoint_ref`，分类使用共享契约的 Host 交互分类。锁定版本 LangGraph 1.2.11 在同一 tools task 内连续触发审批、elicitation 或核对时可能复用框架 interrupt ID，因此 Host ID 不能直接等同框架 ID。

Host ID 按 `(graph_thread_key, checkpoint_ns, checkpoint_id, framework_interrupt_id, kind, logical_request_ref)` 稳定映射，其中逻辑请求引用为本类 `interaction_id / wait_id / operation_id`，内部 `runtime_interrupt_mappings` 保存反向关系。同一节点顺序等待可能同时复用框架 ID 和 checkpoint，必须纳入逻辑请求引用。`inspect_interrupts` 返回指定 checkpoint 的 Host ID；`resume` 先验证 Host ID 集合、run 与 input revision，再将答复映射回框架 ID 传给 `Command(resume=...)`。新逻辑等待不得覆盖已经终结的交互记录。

同一工具节点的多次逻辑等待保存有序 `runtime_tool_waits`。节点重放时先按原顺序重放 `interrupt`，再调用 ToolGateway；新的等待追加到序列，不能跳过原审批直接把旧 resume 值当作新 elicitation 答复。只有工具账本中的持久回复提供授权依据，框架 resume 值本身不授予执行权。
用户交互引用 `interaction_id`，子任务等待引用 `wait_id`，待核对操作引用 `operation_id`；每项仅携带本类必要引用。
引用保留 run、输入版本与规范化参数的业务关联，不把整个工具参数或凭据复制到 interrupt payload。
`inspect_interrupts` 从指定 checkpoint 读取并返回这些引用，不能用 thread 最新交互替代旧 run 的恢复目标。
RuntimeOutcome 的等待结果携带已验证的引用集合，具体映射到哪个 RunStatus 由 Scheduler 决定。

LangGraph interrupt 的恢复会从节点开头重执行，不是从原 Python 指令位置继续。
因此节点在 interrupt 前的准备代码必须可重放；避免在该区间写临时文件、发送请求或注册无幂等保护的对象。
用户编辑参数后由 ToolGateway 重新形成参数 hash 和策略判断；runtime 不沿用旧批准。
用户交互与 child wait 使用可区分的内部等待对象；父子派生、等待登记和唤醒协议完全归 [06](06-scheduler.md)。
取消与审批拒绝保持不同语义；拒绝作为明确工具结果可供模型调整，取消则停止后续调用。

## 7. 退出、故障与最终结算边界

在每次模型和工具调用边界检查取消、deadline、预算和循环信号，命中后返回共享结果，不自行提高上限。
循环检测可先采用“相同工具与规范化参数连续失败三次”的初值，合法轮询使用单独策略。
协议错误、checkpoint 失败和不可处理工具故障保留结构化原因；不吞异常后伪造最终成功回复。
不可确认外部副作用的结果沿工具模块结论返回，不能由模型解释成“应该没有执行”。
`after_agent` 只产生候选结果，它仍处于图调用内部，不能在此写 completed 或发送 run 完成事件。
Worker 确认图退出、最后 checkpoint 已持久、必要产物已登记及子任务已处理后，再按 [06](06-scheduler.md) 完成结算。
runtime 的失败不自动意味着所有工具回滚；返回已登记产物和结果引用，使用户能看到已完成工作。
无法立即停止的调用保留可解释状态，request_cancel 返回后仍需要 Worker 核实运行资源已结束。

## 8. 交付物与完成依据

交付 AgentSpec 校验器、runtime 工厂、LangChainAgentRuntime、middleware 组装器与框架事件转换入口。
提供最小只读工具 Agent 配置、运行状态序列化样例和锁定依赖兼容报告。
提供模型响应保存前、保存后、工具结果返回后和 interrupt 暂停处的故障注入点。
验收执行 [13 · R01、R03、R05](13-verification.md)，保留 checkpoint、ledger、模型调用次数及 run 事件对应证据。
本模块交付标准包含恢复语义；仅展示一轮成功对话不能判定 RuntimeAdapter 完成。

## 9. 设计来源

- [原系统设计 §7](../通用Agent-Harness系统设计文档.md#s7)：运行契约、middleware、持久屏障与 interrupt 约束。
- [原系统设计 §3](../通用Agent-Harness系统设计文档.md#s3)：AgentSpec 配置形态。
- [LangChain / LangGraph 调研](../research/langchain-langgraph.md)：第 3、4、6、7、10 节的框架复用与重放边界。
