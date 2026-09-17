# DeepSeek Harness 官方实现调研

调研日期：2026-09-17。用途：为“基于 LangChain / LangGraph 的通用 Agent Harness”提供参照依据；以下“设计建议”是本项目推导，不是 DeepSeek 的产品承诺。

## 1. 调研基线与证据边界

- 官方仓库为 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)，官方 README 链接到 [文档站](https://deepseek-harness.github.io/deepseek-harness/)。
- 本次 `git ls-remote origin HEAD` 返回 `0d1f50007f9bca3f52b06e1c3074fa14d5fb0720`，与本地只读检出的 commit 一致；commit 日期为 2026-09-15T11:16:06+08:00，提交说明为 `Merge pull request #4192 from deepseek-harness/worktree-bootfast2`。
- 该 commit 的根 `package.json` 版本为 `0.1.6-alpha.1`；这是仓库版本，未将其等同于 npm 的 latest tag。下面链接固定到该 commit，避免 master 漂移。根包引擎声明为 Node `^22.19.0 || >=24.0.0`。[版本文件][D01]
- 官方仍将其标为 developer preview，明确会出现不兼容变更；安全公告声明尚未经过安全审计，不能视为生产就绪。[README][D02] [安全公告][D03]
- 本次研究核对了官方文档、默认 bundle、部分实现源码；未运行完整构建、模型 API、浏览器端到端验收，也未对安全声明作独立审计。不能把文档中“支持”转换为本机验收通过。

## 2. 总体架构与产品形式

DeepSeek Harness 的基础是 **Cordis 插件容器**，并非 LangChain / LangGraph。模型适配器、工具注册表、Session 日志、agent loop、持久化、权限和 UI 能力都通过插件组合；注册具有作用域和可释放的生命周期。新增功能通常挂在 service / event 接口上，而非持续修改主循环。[架构][D04]

运行时由 profile 和 bundle 层叠组合：共享 base 提供模型、工具、持久化、sandbox、审批、settings、credentials；web-app 添加浏览器应用；headless 为单次无服务器运行；sdk 为 JSON-RPC SDK 服务。层叠顺序为 bundles、profile patch、home patch、命令行 patch。Web profile 支持 live patch reload，一次性/stdio profile 在启动时固定组合。[架构][D04]

面向用户的默认形态确实是本地服务加 Web：`npx @deepseek-ai/dsh web` 默认启动 `http://127.0.0.1:3080` 并打开浏览器。本仓库另有 Electron 桌面壳，其通信走私有管道和 `dsh-app://`，不能把桌面壳实现概括成嵌入式 localhost Web server。[README][D02] [架构][D04]

浏览器使用 HTTP POST 发起 unary RPC，API Gateway 的 `/api/remote.mux` WebSocket 承载逻辑流。其设计不是“一切直接 WebSocket”，也不是 SSE。业务服务显式标记 Remote 方法，由构建产物生成 Host/Client 合约及验证器；取消、请求关联、schema 验证和业务 dispatch 各有归属。[API Gateway][D05] [Connection][D06]

**可借鉴：** 产品入口统一、领域能力接口清晰、同一后端驱动 Web/无头/SDK、UI 不持有执行权。  
**不宜照搬：** 自研类型编译器、全插件热重载、大量细粒度 npm 包是已有大型系统的选择；本项目 MVP 更适合模块化单体和少数稳定插件接口。

## 3. 功能现状与能力归属

| 功能 | 当前官方实现与归属 | 已确认限制 |
|---|---|---|
| 本地服务 + Web | 官方 Web profile，localhost 默认入口 | 它首先是本地受信环境的 harness，不能据此假设多租户 SaaS 能力 |
| 模型兼容 | 官方 DeepSeek adapter + llm-pi-ai adapter；内置供应商和自定义网关 | 不是“任意 OpenAI-compatible 地址必然可用”；能力和 wire 差异需要显式配置 |
| 上下文压缩 | base 默认挂载 compaction-basic、tool-result-pruner、image-offload | 无法压缩所有 system/tool schema，也无法任意拆分一个不可分割 tool 单元 |
| Session 持久化/恢复 | 官方 event log、JSONL provider、resume/fork、版本迁移 | durable flush 和内存接收不是同一保证；恢复不等于外部副作用 exactly-once |
| 子 agent | 官方多个 provider，包括 fresh/fork in-process 和外部产品/协议桥接 | 每个 provider 的能力不同，必须能力校验 |
| Agent Teams | 官方发布的 experimental opt-in 包及其 tool 包 | 单进程、共享 checkout；无分布式共识，writeScopes 不是写锁 |
| 基础工具 | 文件、搜索、Bash/PowerShell、持久终端、Web、LSP、任务等官方包 | 包存在、安装进 profile、模型当前可见是三个不同状态 |
| Skills | 官方 registry + filesystem provider + tool-skill；base 提供 | 是可发现、按需读取的指令能力；不是自动获得 OS 权限的插件 |
| MCP | 官方 per-server client，stdio/Streamable HTTP，tools/resources/instructions | 无默认外部 server；不支持 MCP prompts、elicitation、task execution、resource subscriptions |
| 插件扩展 | Cordis + profiles/bundles + scoped registrations | 同进程第三方代码需要信任；热卸载和版本兼容有额外复杂性 |
| 权限/审批 | sandbox 与 approval 两个独立执行维度，再映射 UI preset | 审批 UI 不等于 OS 隔离；Auto 是额外 experimental integration |
| 断线恢复 | Client generation recovery、journal follow、cursor gap repair | token live frame 与持久化 event 不是同一种数据；业务错误不能无限重试 |

依据：[默认 base][D07]、[模型][D08]、[压缩][D09]、[持久化][D10]、[Subagent][D11]、[Teams][D12]、[工具目录][D13]、[Skills][D14]、[MCP][D15]、[权限][D16]、[Session Controller][D17]。

## 4. 底层 LLM 兼容的真实边界

官方模型入口包括 DeepSeek 专用适配器，以及封装 pi-ai 的多供应商适配器。自定义 provider 支持三个明确协议：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。一个 provider 对应一个协议；同一网关提供两种协议时应声明两个 provider。[模型配置][D08]

模型不是只有 `model_id + base_url + api_key`：还涉及 context window、max output、输入模态、reasoning effort、system/developer role、输出 token 字段和供应商特有 thinking 格式。文档专门说明“可连通且有 key”仍可能因 `developer` role 或 `max_completion_tokens` 不兼容而失败。人工加入的模型默认可能只接受文本；能力声明不等于实际探测成功。模型列表发现只是便利功能，失败时仍需手工配置。[模型配置][D08]

模型注册、流式 chunk 词汇、请求准备与失败归一化在独立 LLM 层；agent-loop 不直接认识每一种 wire API。[LLM 源码][D18] Web 页面不支持所有供应商 OAuth 登录；例如“Models 页面尚不支持 Codex OAuth”不能推导成“项目完全不能委派 Codex”，因为外部产品 subagent provider 是另一条集成路径。[模型配置][D08] [Subagent][D11]

**对本项目的含义：** 用 LangChain 提供 model wrapper，但在其上保留自己的 ProviderConfig、ModelCapabilities、错误分类和兼容性探测；至少分别验证 streaming、tool calling、tool-call ID、structured output、usage、cancel、reasoning、vision 和 context overflow。不能把 `init_chat_model` 或一层 `base_url` 透传写成完成兼容工作的证据。

## 5. 上下文、事件与恢复设计

### 5.1 事件日志是事实源

官方明确要求：到达模型请求的信息必须能从 Session log 重建。日志保存 turn、step、system/user/assistant、tool call/result、请求 envelope 等；`deriveMessages()` 从它生成当前模型上下文。UI、fork、resume、telemetry 都读取持久化事实及其 projection。[架构][D04]

正在生成的 assistant token 是 transient live frames；完成/失败/取消的 attempt 在 settlement 后才变成 durable assistant event。硬进程崩溃发生在 settlement 前时，不保证存在该次完整 token 流。文档并未承诺每个 token 已 fsync。[架构][D04] [Session Controller][D17]

### 5.2 持久化不是“每次 append 就绝不丢”

默认 Session provider 是 JSONL，可使用 Zstandard，负责 framing、单 writer ownership、generation 选择和迁移。SessionHandle 的 `append` 表示有序接收与本进程可见，`flush` 才是 crash durability barrier；写后批处理窗口用于降低频繁 I/O。[持久化][D10]

崩溃恢复保留已经落下的有效事件，不丢弃整个未结束 turn。读取取得物理有效的连续前缀；resume 持有 write ownership 后补齐 interrupted turn 的缺失 tool errors、step/end 与 turn/end。只读查询只能在内存补齐，不应悄悄写回。高版本格式不可识别时拒绝，历史迁移生成新的版本文件并保留旧文件。[持久化][D10]

### 5.3 压缩是模型上下文投影的变化

compaction-basic 默认按已路由模型的 context window，在 80% 压力触发、保留最近 16% 原文；支持模型级覆盖和 `/compact`。流程可先无模型调用地裁剪大 tool results，再重测，仍不足才调用 summarizer。默认最多一次 overflow recovery；这些是该 commit 的默认值，不能套用于所有模型。[压缩包][D09]

压缩会记录 start/summary/end、被替代的范围、旧 event 序号、估计 token、summary 模型及 usage；通过新 message 的 replacement surface 操作改变模型可见上下文，而非删除历史事件。边界保护 tool-call/result 配对。压缩锁必须覆盖摘要生成和替换提交；否则崩溃可能留下“已结束但实际上未完成”的假状态。[压缩子系统][D19]

默认 base 中 tool-result-pruner 的文本阈值为 8192 字符、保留前 4096/后 1024；这是该 profile 的配置，不是抽象算法的普遍默认。image-offload 是另一个专门处理图像预算拒绝的机制。[默认 base][D07]

**对本项目的含义：** 将完整审计记录、LangGraph checkpoint、发给模型的 working context、Web 事件流分别定义并明确谁是事实源；不能放四份互相漂移的“聊天历史”。摘要应保留用户约束、已确认决定、未完成项、artifact 引用和工具配对，并可回查原文。恢复需要记录“外部操作结果未知”，不能盲目重放 shell/MCP 写操作。

## 6. 子 agent 与 Teams

官方 Subagent 是可选 capability，而非主 loop 内置分支。注册表允许多个 provider 并存，包括 in-process spawn/fork、ACP、Codex、Claude Code、dsh-sdk。启动能力如 agent options、output schema、depth cap、tool filter、persona 显式声明；调用要求 provider 不支持的能力时应明确拒绝，不接受后静默忽略。[Subagent][D11]

fresh 子 agent 拿任务说明而不继承整个对话；fork 继承父会话已经完成的连续 turn 前缀，不包含父会话正在运行的不平衡 turn。continuable child 由 activation manager 管理身份、初始投递、持久化发现、cold resume 和释放；父子消息投递、当前 turn interrupt 与删除会话是不同操作。delegation depth 和权限来源随持久化恢复，避免重启降低层级限制。[Subagent][D11]

当前官方已有 experimental Agent Teams，不能照旧资料写成“Teams 只有第三方插件”。但它是额外 opt-in，不是 base 默认挂载能力。Team 的 lead 是 root Session；roster、task DAG、mailbox 写入 lead log。任务采用 revision compare-and-set；依赖必须无环；消息先写 queued 并 flush，目标 inbox/history 有 durable 身份后才写 delivered，以重试和去重完成进程内恢复。[Teams README][D12] [Teams 类型][D20] [任务源码][D21]

**明确限制：** 同一团队协调在单一进程，成员共享 cwd/checkout；writeScopes 是路径重叠警告，不阻止写文件；任务 owner 不会自动租约过期；不能据此承诺独立 worktree、多进程调度或 distributed exactly-once。Lead-only 操作和 peer message 身份检查是必须保留的约束。[Teams README][D12]

**对本项目的含义：** LangGraph 可实现 supervisor/subgraph/fan-out，但团队产品还需要 scheduler、资源预算、durable inbox、lease/CAS、取消传播、并发写策略、权限继承和 UI 状态。MVP 可以只做单 supervisor + 有界子任务；共享任务 DAG 和长期 Teams 应作为更后阶段。

## 7. 工具、Skills 与 MCP

### 7.1 统一工具治理

官方工具层统一参数验证、schema、canonical output、timeout、parallel-safety、权限策略和展示。主要 pipeline 为记录 tool call → pre-execute policy → 不可被后续插件放行的 monotonic guards → execute → post-execute → finalize → frozen result → durable tool result。允许、拒绝、请求审批与工具业务错误都有明确结果。[工具层][D22] [执行管线][D23]

目录包含文件读写/编辑、文件搜索、Bash/PowerShell、持久 shell/terminal、Web、LSP、todo、jobs、workflow、session query、subagent、skills 等。还支持 native Function Calling 与 PTC：后者由模型写代码编排工具，内部子调用仍走统一政策路径。是否采用 PTC 是独立模型适配和沙箱决策，不能跳过统一权限。[工具目录][D13] [工具层][D22]

### 7.2 Skills 是按需指令加载

官方 skills 包划分 registry、provider、consumer。初始模型目录只给 model-invocable 的 name/description，正文按需 get；本地 provider 接受目录式 `name/SKILL.md` 和平铺 Markdown，项目/用户/自定义/捆绑路径按明确优先级合并，同 scope 重名要确定性处理。provider 可扩展为 remote，但本次不能据接口推断某个特定在线 skill marketplace 已实现。[Skills][D14]

文件系统监听和写工具观察可使目录失效；不完整 discovery 不当作权威空目录，保留可读候选并重试。modelInvocable 与 userInvocable 是两个独立开关。资源路径相对 skill 的 resourceBase 解析，不能随意借 agent cwd 猜测。[Skills][D14]

**对本项目的含义：** skill 是有来源、版本、摘要、正文、资源和调用规则的内容包；运行脚本仍走普通工具和权限。skill 写“允许联网”不能提升 Host 的网络权限。记录实际加载内容的版本/hash，使 resume 和评测能解释行为。

### 7.3 MCP 是外部能力协议

每个 MCP server 配置一个官方 client plugin；stdio 和 Streamable HTTP 走官方 SDK 的协议协商、分页发现、取消与协议验证。工具公共身份为 serverName 加 raw tool name，避免不同 server 的 search 撞名；更新以整代原子切换，失败保留上一代。[MCP client][D24]

共享 resource service 支持 list resources、list templates、read；按 caller scope 选择已配置 server，内容按需获取。server instructions 作为有来源、有大小限制的文字进入已记录的 system prompt。工具结果保留 canonical MCP JSON，再投影普通 tool content；图像受当前模型能力和附件系统约束，未支持的 rich content 提供诊断，而不是无声丢失。[MCP][D15]

当前不支持 prompts、elicitation、task-based execution、resource subscriptions。client 默认没有外部 server，断线可指数退避重连但有次数上限；连接/发现超时由 SDK 管理，tool/resource call 可单独配置 timeout。[MCP][D15] [MCP client][D24]

**对本项目的含义：** 第一版应声明 MCP capability 支持矩阵，而不是写“支持 MCP”四个字；至少处理进程生命周期、HTTP auth、安全环境变量、超时、资源释放、工具 generation、schema 转换、图片/结构化结果、权限标签和断线状态。

## 8. 权限、本地 Web 信任与插件风险

DSH 将 sandbox mode 与 approval policy 分开，再提供权限 preset。UI preset 层自身不执行工具隔离；真正执行由 sandbox、shell/filesystem providers、approval 和 tools guard 完成。Auto review 是额外集成，其 UI 名称不能替代边界定义。[权限][D16]

Web 并非“监听 localhost 所以不需要认证”。每次进程启动随机 launch token，仅在根路径交换为 authority-bound signed cookie；RPC 和 WebSocket 都需要浏览器 session。Host allowlist、Origin 同源、cross-site 检查在认证前抵御 DNS rebinding/跨站请求；它们不等同身份认证。当前文档明确 `dsh web --host 0.0.0.0` 不受支持。[Connection][D06]

Models 页面写入的 key 是 write-only 展示，后端存入 `$DSH_HOME/.credentials.yaml`，settings 留 credential reference。脱敏展示不等于磁盘加密；新系统可用 OS credential store，并限定日志与子进程环境继承。[模型配置][D08]

“所有东西都能插件化”不是安全边界。加载的同进程插件可能访问 Host 资源；第三方 MCP server 和 skill 内容也应视为外部来源。官方安全公告明确要求不能只依赖 DSH 作为不受信 workload 的唯一保护。[安全公告][D03]

**对本项目的含义：** 本地单用户 MVP 仍应默认 loopback + browser auth + Origin/Host 校验；远程/局域网访问是单独 deployment mode。权限必须在工具执行点检查；审批恢复需绑定 tool args hash、policy version 与实际目标；插件安装默认可信代码模式，不应声称可安全运行恶意 Python 插件。

## 9. 最值得转化为本项目设计决策的内容

1. 用 LangGraph 负责 durable execution 和节点编排；Harness 自己负责领域协议、预算、权限、工具生命周期和产品体验。
2. 明确 thread/session、run、turn、step、message、tool invocation、checkpoint 和 transient stream，避免一个 session_id 代表所有东西。
3. 所有进入模型的上下文可回溯；原始 tool output 进入 artifact store，可见上下文用预算受限的引用和摘要。
4. 注册接口与具体 provider 解耦；模型、文件、进程、MCP、skills、subagent 都需要能力声明。
5. 先完成 single-agent 的恢复、取消、流式 UI、审批和故障分类，再扩展多 agent。
6. 工具 side effect 至少区分 read-only、idempotent、non-idempotent、unknown outcome；checkpoint 不自动提供 exactly-once。
7. 多 agent 的预算和权限只能按父级约束缩小；独立工作区或文件写入串行化必须有实际机制。
8. 以脚本化 conformance probes 和故障注入验收兼容性；“源码支持”与“接入此供应商验收通过”分开报告。

## 10. 固定版本官方证据索引

以下全部固定在 commit `0d1f50007f9bca3f52b06e1c3074fa14d5fb0720`；可用仓库网页查看对应原文与源码。

[D01]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/package.json
[D02]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/README.md
[D03]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/SAFETY.md
[D04]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/architecture.md
[D05]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/api-gateway.md
[D06]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/client/connection/README.md
[D07]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/bundle/base/cordis.patch.yml
[D08]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/user/guide/providers.md
[D09]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/compaction/compaction-basic/README.md
[D10]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/persistence.md
[D11]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/subagent.md
[D12]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/experimental/agent-team/README.md
[D13]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/tool-catalog.md
[D14]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/skills.md
[D15]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/mcp.md
[D16]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/permission-presets.md
[D17]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/api/session-controller/README.md
[D18]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/llm/llm/src/index.ts
[D19]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/compaction.md
[D20]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/subsystems/agent-team.md
[D21]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/experimental/agent-team/src/task-board.ts
[D22]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/core/tools/README.md
[D23]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/docs/tool-execution-pipeline.md
[D24]: https://github.com/deepseek-ai/deepseek-harness/blob/0d1f50007f9bca3f52b06e1c3074fa14d5fb0720/packages/mcp/mcp-client/README.md

