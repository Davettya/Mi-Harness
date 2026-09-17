# LangChain / LangGraph / Deep Agents 技术调研与复用边界

查阅日期：2026-09-17，北京时间。本文是通用本地 Agent Harness 系统设计的研究附录。结论依据 LangChain 官方文档和官方 GitHub；“建议”是针对本项目的设计判断，不代表框架原生保证。未执行安装、兼容性或故障注入实验，因此不把文档中的支持声明等同于本项目已验证结果。

## 1. 核心结论

建议采用 **Python 本地服务 + FastAPI + 独立 Worker + LangChain `create_agent` + LangGraph 持久化**。`create_agent` 返回 LangGraph 编译图，作为运行时中唯一的模型—工具循环；应用自行负责任务队列、进程生命周期、工作区、权限、事件日志、审计和 Web 产品。需要确定性前后处理、分类路由或并行工作流时，再把该 Agent 图放入外层 `StateGraph`。外层图不重复实现 ReAct 循环。

这一组合具有明确的官方依据：middleware 在 `create_agent` 生成的图内工作，官方支持把整个 Agent 作为更大 `StateGraph` 的节点或子图。它保留了中间件能力，又允许自定义周边工作流。[Middleware overview](https://docs.langchain.com/oss/python/langchain/middleware/overview)

不建议从第一版就用原始 `StateGraph` 重写全部模型调用、工具调用、压缩、重试与人工审批协议。原始图应当是“现有 Agent loop 不能表达所需控制”时的升级方案。也不建议把 Deep Agents 当作一组完全无耦合的工具，机械叠加到已有 Agent 上：它本身就是包含上下文管理、文件、Skills、子 Agent 等约定的 Harness 工厂。源码可看到它组装 middleware 并交给 `create_agent`。[Deep Agents graph.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/graph.py)

## 2. 三个开源层次和一个产品运行时

| 层次 | 已有能力 | 对本项目的定位 | 不能据此推定的能力 |
| --- | --- | --- | --- |
| LangChain | 模型与工具抽象、Agent 工厂、消息、结构化输出、中间件 | 标准 Agent loop 与模型/工具集成 | 任意“OpenAI 兼容”端点具备相同语义；跨供应商推理状态可直接续接 |
| LangGraph | 有状态图、分支与循环、checkpoint、interrupt/resume、子图、streaming | 执行状态与恢复的基础设施 | 宿主进程存活、可靠后台队列、终端进程管理、全局资源调度 |
| Deep Agents | 在上层组装文件系统、规划、上下文压缩、Skills、子 Agent 等 | 快速原型或可选完整 RuntimeAdapter | 安全沙箱、产品级权限、可控副作用、零成本接入现有自定义循环 |
| Agent Server | assistants/threads/runs API、任务队列、租约、持久化、流式响应等 | 可替代自行维护应用运行服务的部署方案 | 它等同于 MIT 许可的 LangGraph 库、无外部依赖的轻量单机发行包 |

LangGraph 的官方定位是长期运行、有状态工作流的底层编排；Python 与 JS/TS 都有正式实现。[LangGraph Python](https://docs.langchain.com/oss/python/langgraph/overview)、[LangGraph JS](https://docs.langchain.com/oss/javascript/langgraph/overview)

Agent Server 当前文档在 LangSmith Deployment 下说明，其服务层会把 run 入队、由 worker 获取租约运行，并限制同一 thread 同时最多一个 run。PostgreSQL 保存资源及运行数据，Redis 负责临时信号及发布订阅。这些是 Server 的附加实现，不能写成开源 `StateGraph` 自带的行为。[Agent Server](https://docs.langchain.com/langsmith/agent-server)

## 3. `create_agent` 与 `StateGraph` 的选择

### 3.1 建议默认模式

```text
FastAPI 接收消息并创建 Run
    → 应用持久队列 / Worker 获取 thread 租约
    → 注入 immutable RunContext、模型与工具配置快照
    → create_agent 生成的唯一 Agent 图
        → ContextMiddleware 组装每次模型请求
        → LangChain 模型适配器
        → ToolGatewayMiddleware / 注册工具代理
        → ToolGateway 执行、审批、幂等核对、结果落盘
    → LangGraph checkpoint + 应用 event journal
    → SSE / Web UI
```

`create_agent` 支持运行时动态模型和工具集合、结构化输出、自定义状态及 middleware。动态发现工具时需要同时处理“暴露给模型”与“实际执行”：只向模型补充 schema，而没有相应 `wrap_tool_call` 执行处理，是不完整的集成。[Agents](https://docs.langchain.com/oss/python/langchain/agents)

**本项目的工程建议：**先在 run 启动时冻结工具目录版本与模型配置；一轮 run 内仅筛选已注册工具。确实需要实时 MCP 工具发现时，才增加动态注册路径，并把工具解析结果固定为 `server_id + tool_name + schema_hash + registry_revision`，防止审批后工具含义改变。

### 3.2 哪些需求值得使用外层 `StateGraph`

明确的输入分类、执行前检查、指定角色流水线、受限 fan-out/fan-in、业务级验证与补偿等，适合用显式图表达。LangGraph 的 `Send` 能对运行时产生的任务列表做动态分发，`Command` 能结合状态更新与路由，reducer 决定并发更新如何合并。[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)

**本项目的工程建议：**图节点可以委托独立 Agent；任务是否允许并行、是否具有相互依赖，应由调度策略判断。不要把“模型输出了三个子任务”直接等同于“应并发运行三份代码写入同一工作区”。同文件写入需串行、显式锁或隔离 worktree。

### 3.3 原始 `StateGraph` 的合理升级条件

当产品必须在模型和每一个工具调用之间插入自定义、可独立恢复的图节点；或需要严格控制工具串并行、特定回合终止、供应商特殊消息转换；且 `create_agent` hooks 无法表达时，可以改为自建图。

升级成本包括维护模型—工具消息配对、动态工具分发、结构化输出重试、错误转换、HITL、上下文压缩顺序等。**LangChain `AgentMiddleware` 不是任意 `StateGraph` 的通用插件。**可复用方式是嵌入完整 Agent 图，或为原始图显式实现等价节点/包装器；不能在设计图上写一个 `StateGraph(middleware=[...])` 就视为完成。[Custom middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)

## 4. Middleware 可复用点与单一所有权

| 能力 | 官方复用点 | Harness 必须补充的约束 |
| --- | --- | --- |
| 调用前上下文组装 | `before_model`、`wrap_model_call` | token 预算、隐私域、模型能力与模板版本 |
| 模型路由 | `wrap_model_call` | 能力匹配、额度、跨供应商数据去向、切换后上下文兼容 |
| 工具控制 | `wrap_tool_call` | ToolGateway 权限、schema 校验、幂等账本、取消、产物 |
| 摘要压缩 | `SummarizationMiddleware` | 原始会话归档、不可压缩字段、事实验证与重新读取 |
| 错误与重试 | Tool/Model retry 和 error middleware | 重试预算只归一个层级；副作用工具不能默认重试 |
| 人工参与 | `HumanInTheLoopMiddleware` | 审批身份、参数摘要、有效期、审批修改与撤销 |
| 限流/终止 | Model/Tool call limit | 全 run token、金额、时间、Agent 深度及并发预算 |

这些已有中间件降低基础开发量，但执行策略仍需产品层选择。[Prebuilt middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

**本项目的工程建议：**让 `ContextMiddleware` 成为上下文策略的唯一入口，内部选择一个摘要实现；ToolGateway 是执行副作用的唯一入口。不要同时开启 Deep Agents 默认摘要、自建摘要和 LangChain 摘要，也不要让 provider SDK、middleware 与 worker 都按各自最大次数重试。应记录实际尝试数，并把总调用数限制在统一预算内。

## 5. 记忆、上下文与 checkpoint 的正确边界

### 5.1 三种持久信息应拆开

1. **线程状态 / checkpointer：**当前消息、执行位置及用于继续本线程的状态。
2. **跨线程记忆 / store：**应用定义的偏好、事实及共享知识。它是数据存取接口，不自动决定应记什么、何时过期或是否可信。
3. **事件与原始产物 / Harness 自有存储：**用户原文、完整工具响应、审计记录、附件、文件版本与事件序号。它们不能只依赖已压缩的 `messages`。

Checkpointer 与 Store 的线程内/跨线程分工是官方明确区分；`InMemorySaver`/`InMemoryStore` 不是可重启存储。[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Long-term memory](https://docs.langchain.com/oss/python/langchain/long-term-memory)

短期记忆可以裁剪、删除、摘要；但删除过程中仍需保持工具调用与工具结果的合法配对。[Short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory)

### 5.2 本项目建议的状态最小集

```text
messages                 当前可继续的工作历史，允许压缩
active_goal              当前目标与已确认约束
plan                     任务计划及状态
artifact_refs            完整数据的引用
pending_approvals        已发起但未决的审批标识
child_task_refs          子任务标识、关系和状态摘要
context_revision         上下文构造/摘要版本
budget_snapshot          剩余预算的快照
```

不可把任务身份、授权状态、子任务 ID、计费账本只埋在自然语言消息中。消息一旦压缩，这些字段就不应消失。工具响应宜保存为产物，再把小摘要和可读取引用交给模型；长期记忆读取应按 workspace/user namespace 隔离，并携带来源、更新时间、置信度和删除机制。

## 6. Durable execution 并非外部副作用 exactly-once

LangGraph 在 super-step 保存完整 checkpoint，也保存同一 super-step 已完成节点的 pending writes。`sync` 在下一步前持久化，`async` 边继续边写，`exit` 只在执行退出时保存；进程故障下的恢复能力因此不同。本地第一版可用持久 SQLite checkpointer；多 Worker/多用户场景使用 PostgreSQL，并在版本锁定后验证后端能力。[Checkpointers](https://docs.langchain.com/oss/python/langgraph/checkpointers)

官方 Functional API 明确指出：任务开始但未成功完成时仍可能重执行，应采用幂等键或检查已存在结果。[Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)

**本项目必须据此定义的故障模型：**

```text
1. ToolGateway 开始调用外部服务
2. 外部服务已经产生副作用
3. 进程在本地结果/checkpoint 提交前崩溃
4. 恢复后无法仅凭 checkpoint 知道步骤 2 是否成功
```

因此队列的单次领取、数据库租约和 checkpoint 都不足以证明“发邮件/下单/提交一次且仅一次”。建议为副作用维护 `prepared → executing → succeeded/failed/unknown` 状态，使用稳定操作 ID；目标服务支持时传递同一幂等键。对 `unknown` 先对账，不得自动再次执行。对于不支持幂等或查询的不可逆工具，需要人工核对。

租约过期后旧 Worker 可能仍在运行。应用需要 fencing token 及 ToolGateway 每次执行前的租约核对；但这仍不能撤销已经送达外部服务的请求。不要承诺数据库 fencing 能阻止所有外部重复副作用。

Agent Server 的部署文档把后台任务队列状态描述为具有 exactly-once 语义；这一表述不能扩展为所有工具外部副作用的 exactly-once。项目文档应明确这两个范围。[Standalone Server](https://docs.langchain.com/langsmith/deploy-standalone-server)

## 7. Interrupt、Resume、取消与人工审批

`interrupt()` 要求可持久 checkpointer 与相同 `thread_id`，用 `Command(resume=...)` 继续。**恢复会从触发 interrupt 的节点开头重新执行**，节点中 interrupt 前的代码会再运行。并行多个 interrupt 时应按 interrupt ID 匹配响应。[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

**本项目的工程建议：**

- 审批请求先固化成可审阅对象，包含工具标识、schema 版本、参数 hash、工作区、影响对象和到期时间，再触发中断。
- 用户拒绝、编辑或过期必须走独立状态路径；编辑参数后重新校验、必要时重新审批。
- pause-for-approval、用户 stop、worker crash 是三个不同语义。Stop 应传播到模型流、子 Agent、MCP 请求与宿主子进程；无法保证取消的外部动作标记为 `unknown` 或 `cancel_requested`。
- 不应把异步任务从 HTTP 请求生命周期中直接 `create_task()` 后便称为可靠后台执行。进程重启恢复依赖持久 run queue 和恢复扫描。

## 8. Streaming 与 Web 事件协议

LangGraph 支持 `messages`、`updates`、`values`、`custom`、`checkpoints`、`tasks` 等流模式；当前官方推荐新应用使用 event streaming typed projections。页面说明该接口在 LangGraph v1.2 引入，传统 stream 模式仍有文档支持。[Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)

**本项目的工程建议：**Web 前端不直接绑定某个 LangGraph chunk 形状。通过 `RuntimeEventAdapter` 转换为自有事件，例如 `run.started`、`message.delta`、`tool.started`、`tool.output_ref`、`approval.required`、`context.compacted`、`run.finished`。事件应有递增序号、run/thread/agent/tool_call ID、时间和 schema 版本。

Token delta 可合并缓冲并用最终 message snapshot 恢复；业务状态事件和审批事件必须持久化。SSE 重连从事件序号恢复，先回放再订阅；浏览器断开默认不取消运行。仅有 `astream()` 并不自带应用级断线重放、幂等消费和事件保留期。

## 9. 多 Agent 和进程调度不是同一层

LangGraph 子图支持 per-invocation、per-thread 与无 checkpoint 三类状态生命周期。独立委托通常适合 per-invocation；同一有状态子图被并发调用时会涉及 checkpoint namespace 冲突，不能忽略。[Subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)

多 Agent 的收益主要来自专业提示、上下文隔离和独立能力；并非每个复杂任务都需要多个 Agent。[Multi-agent](https://docs.langchain.com/oss/python/langchain/multi-agent)

**本项目的工程建议：**第一版限制为主 Agent + 有界子 Agent，显式深度和并发上限。每个子任务有独立预算、工具范围、输出契约及最终产物引用；父 Agent 只接收结构化结果。调度层负责排队、优先级、资源占用、取消传播和崩溃回收；图层负责逻辑依赖、路由和状态合并。两层分别测试。

Deep Agents 的同步子 Agent 会阻塞主 Agent 直至完成。当前还提供异步子 Agent，可启动、查询、更新、取消后台任务；它依赖实现 Agent Protocol 的服务，使用独立 thread/run，不能视为裸库自动提供一个本地后台进程管理器。[Subagents](https://docs.langchain.com/oss/python/deepagents/subagents)、[Async subagents](https://docs.langchain.com/oss/python/deepagents/async-subagents)

如果后续选择 Agent Server，可评估直接复用 AsyncSubAgentMiddleware；若采用自有 Run API，则先定义自己的 AgentScheduler 接口，或明确实现所需 Agent Protocol 子集后再接它。不要表面接入 middleware，内部却将它要求的后台运行语义退化成一次普通 HTTP 调用。

## 10. Deep Agents 可复用与替换成本

### 10.1 已覆盖的 Harness 功能

Deep Agents 工厂可配置模型、工具、middleware、子 Agent、backend、Skills、记忆与 HITL；这些是减少首版开发量的重要候选。[Customization](https://docs.langchain.com/oss/python/deepagents/customization)

Skills 使用 `SKILL.md` 及附属脚本/参考/资源，按目录加载元信息，再按需读取完整内容，符合渐进披露思路。[Skills](https://docs.langchain.com/oss/python/deepagents/skills)

其上下文管理包含大型工具输入/输出卸载与历史摘要，保留文件引用和可搜索的历史文本。文档给出默认触发值，但这些数值应视为所选版本和模型 profile 的配置行为，不能成为本 Harness 固定不变的系统契约。[Context engineering in Deep Agents](https://docs.langchain.com/oss/python/deepagents/context-engineering)

### 10.2 文件 backend 的安全边界

`StateBackend` 面向线程状态；`StoreBackend` 面向持久 Store；`FilesystemBackend` 映射宿主文件；`CompositeBackend` 可分流内部产物和项目目录。`FilesystemBackend(root_dir=...)` 不代表自动有安全边界，官方特别要求路径限制场景配置 `virtual_mode=True`。`LocalShellBackend` 在宿主机执行 shell，该路径模式不能限制 shell 访问。[Backends](https://docs.langchain.com/oss/python/deepagents/backends)

**本项目的工程建议：**文件访问策略与进程隔离分别设计；宿主工具需要规范化路径、符号链接/junction、UNC/盘符、工作目录及权限检查。需要真正运行不可信代码时使用操作系统/容器隔离。内部日志和卸载产物置于用户数据目录，不应与工作区源代码混放。

### 10.3 建议采用“二选一 Runtime”，而不是重复叠加

| 方案 | 优势 | 主要代价 | 建议 |
| --- | --- | --- | --- |
| `create_agent` + 自有 Context/ToolGateway/Skill Registry | 权限、消息、上下文和 UI 语义由 Harness 控制；仅维护需要的机制 | Skills 生命周期、产物引用、子任务产品层需自行补齐 | 默认主线 |
| `create_deep_agent` + 定制 backend/middleware | 规划、文件、Skills、压缩、子 Agent 集成更快 | 接受或替换默认工具、提示、状态、权限和压缩行为；升级需回归 | 并行 PoC / 可选 RuntimeAdapter |
| raw `StateGraph` 自建 agent loop | 控制最细，图节点边界完全自主 | 开发和兼容成本最高，容易重复框架已有机制 | 特殊需求才升级 |

两种 Runtime 可以共享模型配置、ToolGateway、凭证、产物库与应用 Run API，但应分别定义状态 schema/version 与迁移方式，不能承诺任意 checkpoint 互相恢复。用同一功能验收集比较后再决定是否以 Deep Agents 替换主线。

## 11. MCP 新旧适配器与连接生命周期

2026-09-17 实时官方页面已经主推 `langchain.mcp.MCPAdapter`，底层为 FastMCP；页面明确它要求 `langchain[mcp]>=1.4.0`，并处于 **beta**。这是当前核实到的接口门槛，不是本项目已验证或已锁定版本。[MCP overview](https://docs.langchain.com/oss/python/langchain/mcp)

官方迁移说明也表明，新接口并非旧包所有功能的直接一比一替换：`get_prompt`、`get_resources` 不由新 LangChain 适配层提供，完整 MCP prompts/resources 需走底层客户端。[Migration guide](https://docs.langchain.com/oss/python/migrate/langchain-mcp-adapters)

旧 `langchain-mcp-adapters` 官方仓库说明，`MultiServerMCPClient` 默认每次工具调用建立一个 `ClientSession`；有状态服务需要显式 `client.session(...)`，并在有效 session 内运行相关工具。[langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters)

新 MCPAdapter 的工具持有可重入 client，每次调用进入/释放连接；若希望同一 run 复用 session，应让 adapter context 覆盖整个 agent 调用。当前 Connections 文档把 MCP `2026-07-28` 及以后的协商称为 modern era，FastMCP 按连接协商；多个 server 放入同一 MCPConfig 会共享 era，ClientGroup 可保持各自协商结果。[Connections](https://docs.langchain.com/oss/python/langchain/mcp/connections)

**本项目的工程建议：**

- `MCPConnectionManager` 明确连接策略：per-call、per-run 或受控长连接；按 server、用户、凭证与 workspace 隔离，不跨身份共用会话。
- 新项目优先验证当前 MCPAdapter/FastMCP 路线，并通过内部接口隔离 beta API；legacy adapter 仅作为经验证的兼容后端，不将旧搜索摘要误写为当前唯一推荐。
- prompts/resources 由 MCP manager 提供独立操作，tool registry 仅负责工具；协议支持和 LangChain 包装层能力分开列出。
- 状态 checkpoint 不能序列化活跃 socket、stdio 子进程或内存 session。恢复时重新建连接，验证服务版本与工具 schema；服务端临时状态丢失需显式报错或重建。
- stdio MCP 子进程的 stdin/stdout 归协议，stderr 归日志；生命周期、超时、进程树退出和敏感环境变量由 Harness 管理。

## 12. Python 与 TypeScript 选择

官方同时提供 LangChain JS `createAgent`、LangGraph JS 和 Deep Agents JS。因此选择 Python 不能建立在“TS 没有这些核心能力”的错误前提上。[LangChain JS Agents](https://docs.langchain.com/oss/javascript/langchain/agents)、[deepagentsjs](https://github.com/langchain-ai/deepagentsjs)、[langgraphjs](https://github.com/langchain-ai/langgraphjs)

| 维度 | Python 服务 + TypeScript Web | TypeScript 全栈 |
| --- | --- | --- |
| 适用团队 | Python/AI/数据工具为主 | 前端/Node 团队为主 |
| 接口一致性 | 需 OpenAPI/JSON Schema 生成客户端 | 共享类型方便，但运行时校验仍不可省 |
| 本地工具 | 便于直接复用 Python 数据与文档处理工具 | Node 工具链与前后端开发统一 |
| Python Skills | 同语言调用较简单 | 通常通过子进程调用 Python |
| Windows 分发 | 要管理 Python 环境与依赖 | 要管理 Node 运行时；调用 Python skill 后仍可能双环境 |
| 框架演进 | 按 Python 文档与包版本验证 | 必须按 JS 文档验证，不能套用 Python 参数与 beta 能力 |

上表是工程取舍而非性能实测。针对通用本地 Harness，建议 Python Agent 服务、TypeScript/React Web、JSON Schema/OpenAPI 作为契约；不要同时实现 Python 与 TS 两个 Agent 内核。若团队实际主要掌握 TS，选择 TS 也合理，但 MCP、checkpointer、middleware、Deep Agents 所需能力应分别做包级核验。

## 13. 自建 FastAPI + Worker 与 Agent Server

| 项目 | 自建最小服务层 | Agent Server |
| --- | --- | --- |
| 本地首次启动 | 可只需进程 + SQLite + 用户数据目录 | 文档方案涉及 server 镜像、PostgreSQL、Redis |
| Agent loop | 仍复用 `create_agent`/LangGraph | 运行已部署图 |
| run queue/租约/并发 | 自己实现并故障测试 | 已有服务层实现 |
| SSE 与恢复 | 自有稳定产品协议与事件 journal | 复用 Server API，UI 做适配 |
| Workspace/终端/Skills/权限产品 | 自己实现 | 仍需产品层实现或扩展 |
| 离线与发行控制 | 由自身依赖和功能决定 | 需核查发行许可、license 验证与离线配置 |
| 扩展为团队部署 | SQLite→PostgreSQL，增加 Worker | 官方部署路径与扩缩容能力较完整 |

Agent Server standalone 文档列出 license key 与特定情况下的许可验证/用量报告外联，并区分 Docker 本地开发与 Kubernetes 生产路径。故不能未经核实把它当作“pip 装 LangGraph 后免费离线运行的同一组件”。另一方面，LangGraph 库官方仓库是 MIT 许可；两者应分别评估。[Standalone deployment](https://docs.langchain.com/langsmith/deploy-standalone-server)、[LangGraph LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)

**本项目的工程建议：**第一版选择自行维护最小 run 服务，但严格控制边界：做 durable queue、worker lease、thread 单写、取消和事件，而不自研图引擎/checkpointer。将 `RunBackend` 抽象成应用接口，未来可用 Agent Server 实现同一接口；迁移评估应计算自研运行服务维护成本与 Server 运维/许可成本。

## 14. 最终复用 / 自建矩阵

| 功能 | 默认决策 | 自建边界 | 验收重点 |
| --- | --- | --- | --- |
| LLM 消息及调用 | LangChain provider adapter | ProviderProfile、能力探测、兼容例外 | 流式工具、JSON、取消、usage、上下文溢出 |
| Agent loop | `create_agent` | 中间件组装与角色配置 | 唯一循环、终止与预算 |
| 复杂流程 | 必要时外层 `StateGraph` | 图拓扑与状态契约 | 并发 reducer、失败与恢复 |
| Checkpoint | 官方 SQLite/PostgreSQL saver | 版本迁移、备份、保留策略 | 重启、损坏恢复、数据兼容 |
| 长期记忆 | Store 接口 | 写入策略、隔离、来源、过期/删除 | 不误记、可追溯、可撤销 |
| Compaction | LC 摘要或受控 Deep Agents 实现二选一 | ContextMiddleware、原文归档、保护字段 | 工具配对、目标保留、小上下文模型 |
| 工具 schema | LangChain Tools | ToolGateway、权限、副作用 ledger | 参数校验、审批、unknown 对账 |
| Skills | 可借鉴/复用 SkillsMiddleware | Registry、发现、信任、版本、权限 | 按需加载、更新失效、脚本执行控制 |
| MCP tools | MCPAdapter/FastMCP，内部封装 | ConnectionManager、工具快照、凭证隔离 | 新旧协议、session、断线、schema 变化 |
| MCP prompts/resources | 底层 MCP client | 映射为资源及模板，不自动当工具 | 类型、大小、权限与生命周期 |
| 子 Agent | 子图或独立 run | Scheduler、预算、任务关系和取消 | 并发上限、孤儿回收、结果可追踪 |
| Worker 与队列 | 最小自建持久队列 | 全部应用生命周期 | 崩溃、租约、fencing、无双写 |
| Web streaming | LangGraph stream 数据源 | EventAdapter、SSE replay、UI | 重连、去重、不同 Agent 归属 |
| 可观测性 | 可选 LangSmith/标准遥测 | 本地审计和隐私控制 | 离线可用、凭证脱敏、trace 关联 |

## 15. 开工前应完成的四个验证实验

1. **唯一 loop 实验：**两个模型供应商与一个本地模型分别跑 `create_agent + ContextMiddleware + ToolGateway`，验证动态工具、结构化结果、工具失败、模型中断。未具备工具调用的模型只能获得受限能力，不可标为完整 Agent 兼容。
2. **恢复实验：**在只读工具之后、外部写成功但本地提交之前、等待审批时分别强制杀 Worker；观察 checkpoint、ledger、事件及任务状态的一致性。确认 unknown 不自动重试。
3. **上下文实验：**用小窗口模型驱动长对话、大工具输出、并行子任务；验证摘要后目标、审批、子任务 ID、产物引用不丢失，并验证原文可检索。
4. **MCP 实验：**stdio 与 Streamable HTTP，各覆盖 legacy/modern 服务、per-call/per-run session、资源/提示、连接中断及 schema 更新；版本锁定应以这些实验通过为准。

依赖清单最终应记录具体 release/tag、锁文件、操作系统与兼容结果。文档不以滚动官网中的模型名称或“latest”依赖作为可复制构建依据。
