# 开发实施计划与证据索引

基线：01–13 实施规范，P0 + P1；P2 仅保留扩展点。开始日期：2026-09-17。

## SDD 工作规则

先读取唯一所有者规范，建立类型和验收映射，再编码和验证。不得以模拟通过替代真实供应商或外部服务验收；规范冲突先记录决策再修改实现。现有原始设计与研究快照保留。

## 实现组织

规范允许将拟议 packages 目录先组织成单个 Python 工程。本项目采用 `harness/{core,storage,artifacts,policy,platform,tools,scheduler,model_gateway,context,runtime,skills,plugins,mcp,server}`，Web 使用 `apps/web`。这只改变目录映射，不改变模块权威或依赖方向。

| 批次 | 内容 | 对应规范 / 验证 |
|---|---|---|
| G0 | 公共 DTO、依赖锁、框架/Windows/MCP 探针 | 01、12、POC01–04、CT01 |
| G1 | SQLite 仓储、产物、策略、凭据、工具账本 | 02、07、12、DB01–02、T01–02、SEC01–02 |
| G2 | 模型、上下文、运行内核、调度、API、Web 纵向闭环 | 03–06、10–11、M01–03、C01–03、R01–06、U01 |
| G3 | Skills/插件/MCP、配置、诊断、启动与备份 | 08–09、12、S01–02、P01–04、DG01、UI01、D01、PF01 |
| G5 | 子任务、分支、steering、记忆、MCP 增强 | A01–04、CX04、MP05、UI02 |
| G4 | 全量冒烟、故障恢复、构建发行、文档与实现核对 | 13 全部适用项，区分本地实测与外部待验收 |

## 模块协作

公共 DTO 只在 `harness.core` 定义。持久化和 SQLite SQL 只在 `harness.storage`；领域服务通过仓储端口访问。同步短事务不得跨越网络、模型、工具执行或人工等待。运行内核只返回 RuntimeOutcome，调度器拥有状态结算。所有工具均进入 ToolGateway，副作用先检查同步 checkpoint 和合法执行身份。

三个独立开发分工：模型/上下文/运行内核；Skills/插件/MCP；Web 工作台。主开发负责公共契约、持久化、策略、工具、调度、HTTP、监督进程和集成验收。共享接口变动需先同步所有调用方。

## 验证记录规则

验收定义仍由 13 唯一维护。本页最后将追加功能实现核对清单，逐项关联实现、测试和实际结果。未验证、受外部依赖限制、建议性能目标均单列，不勾选为通过。

## 2026-09-17 实施结果与范围

从纯文档工程实现了本地 Python 服务、独立 Worker、React 工作台以及 P0/P1 模块。用户在最终验收阶段明确选择“先完成本地验收，真实服务稍后配置”；下表的勾选表示实现及相应本地证据齐备，不表示尚未连接的真实供应商、第三方 MCP 或账号已通过。

本地结果以 [统一执行记录](../verification/verification-result.json)、[JUnit 明细](../verification/latest-pytest.xml)、[源码与依赖指纹](../verification/source-manifest.json)、[浏览器验收](../../apps/web/VERIFICATION.md) 为准。工作区无 Git 仓库，用逐文件 SHA256 标识被测代码，不虚构 commit ID。运行模型为明确标记的确定性 Demo；协议测试使用实际 SDK 连接 localhost fixture。

### 关键实现决定

- `harness.core` 维护共享 DTO；`harness.storage` 独占 SQL。业务库与 saver 分库，依据 operation ledger 与持久 checkpoint 恢复，不声称跨库原子提交。
- 官方 MCP SDK 2.2.0 直接提供 modern/legacy 协议适配；没有依赖未经实测的 FastMCP/MCPAdapter beta。对应选择与协议边界记录在 [09](09-mcp.md) 和 [兼容记录](../compatibility/mcp.md)。
- LangGraph 同一个 tool task 可复用框架 interrupt ID，甚至复用 checkpoint。Host ID 结合 graph、checkpoint、框架 ID 和逻辑交互引用，按 [05](05-runtime.md) 持久映射；不覆盖先前批准。
- 分支先持久复制 graph，再复验源 revision 并发布业务分支，固定上下文随分支复制；文件及外部副作用保持原状。
- Windows 命令执行使用 Job 与 PID/创建时间校验的进程树管理；对 Windows Store Python 派生激活另有身份追踪。它属于可信本机执行，不是强沙箱。
- 平台用户配置按 instance 完整传入 API/Worker，默认禁用网络与进程，工作区/Agent/当前策略只能进一步收窄。命名策略、冻结出站上限、即时撤销均进入实际执行路径。
- 产物 GC 提供保守预览，保留所有数据库登记对象及引用；没有自动删除用户内容。备份采用协调停止写入者的维护窗口。

## 功能实现核对清单（追加）

### 公共契约、存储与产物

- [x] 版本化执行身份、诊断身份、状态、checkpoint、交互、事件、产物与错误 DTO；禁止未知字段，耐久/临时事件游标分离。实现：`harness/core`；证据：API/Runtime/Storage 契约测试。
- [x] SQLite WAL、外键、短事务、CAS、唯一 operation/delegation/interaction、worker fence、预算预留/结算/unknown 保留、outbox。实现：`harness/storage/store.py`；证据：`test_storage_tools.py`、`test_scheduler.py`、`test_cross_module_guards.py`。
- [x] 内容寻址原子产物、上传限额、归属检查、范围读取、缺失/损坏报告、引用登记、安全预览与 GC 预览。证据：`test_storage_tools.py`、`test_api.py`、浏览器 HTML 脚本隔离实测。
- [x] 双 SQLite 库、对象与 Skill 快照的协调备份；带版本/hash manifest；新目录恢复、未来版本拒绝、对象引用完整性检查。证据：`test_platform.py`、`test_release_smoke.py`、`test_cross_module_guards.py`。

### 模型、上下文与记忆

- [x] ModelProfile 版本锁、能力证据状态、文本与工具协议、严格分片 JSON、attempt 隔离、完整消息配对、单一重试所有者、费用未知、取消/deadline/全局许可。实现：`harness/model_gateway`；证据：`test_model_providers.py`、`test_runtime_context_gateway.py`。
- [x] OpenAI Chat Completions/Responses、Anthropic Messages、Ollama Chat adapter 路由。实际 SDK 的 loopback 多轮协议与图像载荷验证通过；供应商在线兼容性单列待验收。
- [x] 完整请求估算、硬窗口边界、工具输出卸载、原始档案、ContextView、可解释预算、固定约束、结构化摘要、来源引用、input/context/pin revision CAS。实现：`harness/context`；证据：模块测试与生产 Worker 集成。
- [x] 受控文本附件有界物化；已验证 vision profile 的 PNG/JPEG/WebP/GIF 图像转换；附件归属/hash/大小校验。二进制 PDF/DOCX 提取明确报不支持，不将上传成功伪装成内容理解。
- [x] 长期记忆提议、审阅、接受/拒绝、编辑、到期、遗忘和检索前权限过滤；默认不把模型推测直接存为已接受记忆。证据：模块测试和浏览器全 CRUD。

### Runtime、调度与多 Agent

- [x] `create_agent` 唯一循环、真实 LangGraph SQLite saver、工具前同步 checkpoint、稳定 message/tool-call ID、RuntimeOutcome 与调度结算分离。
- [x] 耐久用户输入与审批、多项 interrupt 屏障、超时不授权、顺序交互逻辑 ID、重启恢复、等待保留分支所有权并释放物理槽。
- [x] FIFO 领取、lease/heartbeat/fence、deadline、取消屏障、后代取消、旧 worker 存活时不重放、准备态工具/交互/预算在终态收尾。
- [x] 文件副作用后进程退出按目标 hash 核对；任意命令副作用后进程退出进入 unknown/needs_review；接受未决只停止，不生成成功或重复执行。证据：`test_recovery_process.py` 两个真实 OS 进程退出窗口。
- [x] 子任务范围/深度/并发/共享根预算、幂等委派、内部等待、outbox 唤醒、单执行槽父子闭环。证据：scheduler/integration/recovery 测试和浏览器任务树。
- [x] Steering 在安全 checkpoint 后确认消费、重复提交不重复生效、排队输入版本可查询；真实 graph 历史分支与独立 pins。证据：`test_integration.py` 和浏览器实际旅程。

### 工具、Skills、插件与 MCP

- [x] 内置文件列表/读取/统计/搜索/写入/补丁、命令执行/轮询/停止、公开网页搜索/抓取、计划、输入/审批、产物创建/读取/预览/列表、档案/记忆搜索、委派/等待、Skill 加载与资源读取。
- [x] 全部工具走 Gateway、固定工具版本、输入 schema、policy、resource fingerprint、批准绑定、grant 单次事务消费、账本结果复用与资源锁；网络重定向重验、固定目标 IP；Windows 文件目录防重命名与链接拒绝。
- [x] 100MB stdout 有界保留并产物化，超时/取消回收受控进程树；未知退出不能假报完成。证据：`test_storage_tools.py`。
- [x] Skill 安全 frontmatter、来源/同名冲突、固定 hash 快照、按需激活、来源信任与实时撤销、资源逃逸拒绝、二进制资源产物化、无自动依赖安装。证据：`test_skills.py`、`test_plugins_integration.py`。
- [x] 声明式插件 manifest 校验、受支持贡献实际接入所属模块、原子失败回滚、run 固定版本、drain 保留活跃引用、重启重建、新旧版共存及终态释放。任意第三方可执行 entrypoint 不在本阶段开放。
- [x] MCP modern/legacy、stdio/Streamable HTTP/legacy SSE、工具目录与安全别名、schema revision、身份分区、分页防循环、重入调用、资源与 Prompt 来源标记、错误分类与受控取消。
- [x] OAuth PKCE/state/issuer/callback 校验、凭据库引用、跨服务凭据拒绝、重建服务后 callback；modern MRTR durable elicitation 与原 operation 单次续接；发送中结果未知不重放。
- [x] Legacy 普通工具可正常调用；无法保证耐久恢复的 legacy 人工输入、sampling/roots/未知扩展明确拒绝或 protocol cancel。远程 Skills 只保留已声明扩展验证入口，不假定任意 MCP 目录等同 Skills。

### API、工作台与平台

- [x] 一次性配对、cookie、Host/Origin/CSRF、无 run 诊断与独立限额、幂等写入、revision/CAS、上传、snapshot、耐久 SSE 补齐、临时流式草稿及错误脱敏。
- [x] 三栏工作台、工作区/会话/配置、模型诊断、Agent/策略、MCP/Skill、声明式插件导入/排空、审批 diff、输入表单、上下文、记忆、产物、分支、steering、任务树和取消；桌面与 390px 窄屏实测。证据：`apps/web/VERIFICATION.md`。
- [x] CLI serve/status/stop/doctor/pair/credential-set/backup/restore；实例归属验证、端口冲突、重复启动、独立 API/worker、维护排空与配置来源解释。证据：platform/config/release smoke。
- [x] 本地内容脱敏指标：排队、模型首 token/总时延、retry、usage/cost 已知或未知、预算、压缩、工具失败/unknown、checkpoint 延迟、恢复、取消进程残留、outbox；默认禁止环境自动开启远程 tracing/proxy。
- [x] OpenAPI 与 TypeScript 生成物、Python/Node 依赖锁、源码启动说明、构建时打包 Web 静态页、36 项中文效果任务定义及评分标准。

## 验收编号与证据覆盖

“本地通过”仅适用于表中列出的测试范围。13 的场景可能还包含外部服务、性能或更大故障空间，不能由单个相邻用例代替。

| 编号 | 当前证据 | 范围说明 |
|---|---|---|
| POC01、R01、R03、R05 | runtime/context 测试；真实 saver、故障钩子、重建恢复、MRTR 测试 | 工具前后持久边界、本地确定性模型；R03 另有 OS 退出测试 |
| POC02、P01–P04 | `test_mcp.py`、MCP compatibility | 两代协议实际本地传输、分页/错误/取消；非公网第三方认证 |
| POC03、T02 | `test_storage_tools.py` | Windows Job/进程树/100MB 输出；非强沙箱证明 |
| POC04、R02 | `test_recovery_process.py` | 两个指定 OS 退出断点，写入后无重复、命令 unknown、核对 CAS |
| CT01、DB01 | storage/runtime/API 测试、生成 OpenAPI、链接检查 | DTO、唯一键、fence、事务与版本 |
| DB02、D01 | artifact/platform/release smoke | 损坏检测、原子对象、协调备份、新目录恢复；GC为预览 |
| M01 | `test_model_providers.py` | 实际三类 SDK 对 loopback fixture；真实服务按用户选择后续验收 |
| M02–M03 | model provider/runtime 测试 | 分片、重试、usage、能力、模型切换边界 |
| C01–C03 | context 测试、附件 SDK wire 断言、100MB 工具测试 | 请求估算/硬窗口/结构摘要/CAS；摘要语义质量待真实模型任务基准 |
| CX04 | memory 模块与浏览器 CRUD | 过滤先于排名、审阅/到期/编辑/删除 |
| R04、U01 | 浏览器刷新/API 重启；API 与 11 项前端测试 | snapshot/游标/乱序/重复/临时帧；未做性能负载验收 |
| R06、A01–A02 | scheduler/integration/recovery tests | lease/fence、单槽父子、原子预算、取消后无新 child |
| A03 | file approval/version conflict、路径资源锁测试 | 有效 expected_hash 拒绝覆盖；没有把 worktree 当沙箱 |
| A04 | scheduler 早到 child/outbox barrier 用例、生产父子测试 | 绑定前完成、重复委派/唤醒 |
| T01、SEC01–SEC02 | storage/tools/API/cross-module/platform/provider tests | 路径/grant/权限/跨域/秘密/自动 tracing；不是第三方渗透认证 |
| S01–S02 | skills/plugins/plugin integration tests | 固定内容、即时撤销、manifest不授予权限、drain与版本引用 |
| MP05 | OAuth/authorization/continuation tests | 本地协议 fixture + 重建服务；现代MRTR可恢复，legacy输入明确受限 |
| DG01 | provider/API/MCP诊断测试 | 独立身份、预算与固定 probe；零真实示例工具执行 |
| UI01–UI02 | `apps/web/VERIFICATION.md` | 真实本地浏览器旅程；在线供应商另验 |
| PF01 | platform、effective config、release smoke | 启停/冲突/归属/配置/备份；未执行自动更新器（CLI无自动更新动作） |

## 尚未验收与后续事项

- [ ] 按用户选择后续配置真实云模型和本地模型，再执行 M01 实际服务兼容、reasoning/multimodal/structured-output 能力及费用验证；当前没有外部模型被自动标成 verified。
- [ ] 连接指定第三方 MCP/OAuth 账号及远程 Skills 扩展 revision 后执行外部兼容验收；fixture 不替代真实账号。
- [ ] 使用真实模型执行 `evals/tasks.json` 的36项自然语言效果基准，录入人工引用/约束评分、完成率、时延及费用。没有以Demo回显制造成绩。
- [ ] 13 中建议的8小时稳定性与20并发 P95 性能目标尚未执行；当前短时通过记录不能证明这些目标。
- [ ] Windows外平台、Python3.12/3.13组合尚未实际验收；P2 Team、计划任务、多worker、强沙箱、任意第三方可执行插件分发按原范围留作后续阶段。

以上未验收项保留状态；本地验收完成不将其改写为供应商生产认证或正式性能承诺。

## 初始本地验收结果（2026-09-17 20:58–21:01）

初始全量执行于北京时间 20:58–21:01，随后按用户新要求进行了下节的模型配置简化。初始结果和源码指纹已归档到 `docs/verification/initial-20260917/`；本节为历史记录，当前实现与结果以下节及最新统一执行记录为准。

| 检查 | 最终结果 | 可复核证据 |
|---|---|---|
| 后端全量测试 | 99 passed，0 failed / error / skipped；pytest 184.18 秒；1 条第三方 TestClient 弃用警告 | [初始日志](../verification/initial-20260917/pytest.log)、[初始 JUnit](../verification/initial-20260917/latest-pytest.xml) |
| 前端回归 | 3 个测试文件、11 项通过 | [Web 验收记录](../../apps/web/VERIFICATION.md) |
| 实际浏览器旅程 | 配对、任务、审批重连、文件与产物、附件、配置、Skill、记忆、分支、steering、父子任务、取消、插件与窄屏完成 | [操作和观察明细](../../apps/web/VERIFICATION.md) |
| Python 编译与正确性检查 | compileall 与 Ruff E9/F821/F822/F823/F811 均 exit 0 | [初始执行记录](../verification/initial-20260917/verification-result.json) |
| 构建 | TypeScript / Vite、wheel / sdist 通过；wheel 内含 Web 与内置 Skill | [初始构建日志](../verification/initial-20260917/build.log)、[Web 记录](../../apps/web/VERIFICATION.md) |
| 独立安装 | 新虚拟环境按锁文件安装；从 site-packages 导入；已安装 CLI 启动 supervisor/API/worker、配对、真实 graph 任务完成、停止均通过 | [初始安装包验收记录](../verification/initial-20260917/wheel-verification.json) |
| 恢复与副作用 | 真实 OS 退出后文件 hash 核对、命令 unknown 不重发；备份恢复到新目录后读取原产物并完成后续回合 | JUnit 中 `test_recovery_process` 与 `test_release_smoke` |

初始 wheel SHA256：`32ef20875a13f895307421a7eaba54c88d23cd1793330b586073e22b8fa6d90e`，仅标识本节历史构建。当前包位于 `dist/`，指纹见 `dist/SHA256SUMS.txt`；启动步骤见[项目 README](../../README.md)。

## 模型设置简化：实现核对清单（2026-09-17 追加）

用户新要求：按所给 CodeBuddy/WorkBuddy 界面将模型接入简化为提供方、API Key、模型名称；修改规范、重新构建并重启。先更新 03/10/11/12/13 和总体设计的页面契约，再实现以下功能。

- [x] 独立模型表单只展示三项普通输入，地址、adapter/API 模式、配置 ID、能力/预算参数由系统处理；自定义服务单独显示 URL，Ollama 免 Key，其它设置页保留。
- [x] 8 类提供方目录、官方预设与有界发现；可选择或输入完整模型名。提供方地址与参考来源唯一维护，见[预设来源记录](../compatibility/model-setup.md)。
- [x] 临时 Key 不进入普通配置、命令正文、浏览器重试缓存或错误；真实保存写入 OS vault。已存 Key 不回显，同提供方/地址留空保留，轮换保留旧 run 的凭据引用。
- [x] 保存自动检查文本/多轮工具协议，原子发布 profile revision、精确端点授权及默认 Agent 引用；CAS、维护门禁、固定启动模型冲突与失败回滚明确处理。
- [x] 授权只用于指定 owner/profile/完整地址的模型目的；不扩大网页工具、MCP 或进程权限，仍检查 local_only、主机列表和 SSRF 边界。
- [x] 最多四次有界模型请求：必要协议通过后追加可选流式探针；只有通过才启用流式。生产调用按 profile 能力回退普通响应。
- [x] 修复 create_agent 在 bind_tools 阶段将 Host 输出预算字段泄入供应商 SDK 的兼容问题；该参数仅由 Gateway 消费。
- [x] 保存后真实 Worker 使用新 revision、容纳默认工具完整上下文、实际完成 `list_files` 并结算 completed；证据：`tests/test_model_setup.py`，真实 SDK + 本地 HTTP fixture。
- [x] 模型接入/SDK 模块 12 项通过，API 新旧测试 15 项通过，前端 20 项通过；实际隔离浏览器旅程见 [Web 验收记录](../../apps/web/VERIFICATION.md)。
- [x] 新接口同步生成 OpenAPI 与 TypeScript 类型（44 个路径）。
- [x] 最终全量回归及失败修复、发行包复验、数据备份与重启后页面验收完成，详见下表。

| 检查 | 本次结果与证据 |
|---|---|
| 后端全量及修复 | 首次全量 110 项中 109 通过、1 失败，原始记录保留在 [model-settings-full](../verification/model-settings-full/verification-result.json)。修复 Windows 原子状态文件读取瞬时占用后，[平台/配置/恢复回归](../verification/platform-retest.xml) 10 项通过，含新增持续失败时禁止重复启动的故障注入。两个批次合并覆盖 111 个不同用例，无未解决失败；不是一次全量 111 项运行。 |
| 构建与独立安装 | TypeScript/Vite、Python 编译与正确性检查通过；最新 wheel 在新虚拟环境安装，从 site-packages 导入并完成启动、graph 任务与停止，[安装包验收记录](../verification/wheel-verification.json)。 |
| 实际用户服务 | 先协调排空并备份到 `%LOCALAPPDATA%/LocalAgentHarness-backups/model-settings-20260917-215253`，再重启原数据目录；API、Worker、supervisor 与数据库健康，入口仍为 `http://127.0.0.1:8767/`。[备份](../verification/model-settings-backup.json)、[重启检查](../verification/model-settings-restart.json)。 |
| 重启后页面 | Codex In-app Browser 打开实际用户服务，原配对可用；8 类提供方加载，腾讯云为三个输入，自定义增加 URL，Ollama 无 Key。未输入真实密钥、未修改默认 Demo；页面保留供用户配置。[Web 明细](../../apps/web/VERIFICATION.md)。 |
| 规范同步 | 总体设计、03/10/11/12/13、本清单、README、提供方兼容来源、OpenAPI/TypeScript 类型及验收报告已同步。 |

真实供应商账户尚未配置，本次不声称这些账户的连通、费用或效果通过。

## Fake-IP 出站兼容修复（2026-09-17 追加）

- [x] 复现 DeepSeek 域名解析为 `198.18.0.88` 导致 `SSRF_DENIED`；修复前未到达密钥认证阶段。
- [x] 先补充 12/13 的安全边界及验收场景，再实现仅限已授权官方 HTTPS 模型端点的 Fake-IP 兼容；Clash 和其它应用配置未修改。
- [x] 新增 23 项边界用例；连同模型接入、SDK、API 和平台配置回归共 44 项通过，见 [日志](../verification/fake-ip-regression.log) / [JUnit](../verification/fake-ip-regression.xml)。本次为定向回归，不将前次全量结果改写为本次执行。
- [x] 本机真实 DNS + 生产有界 HTTP transport 验证通过策略检查、TLS 校验并收到未认证的 HTTP 401；未发送用户 Key，见 [网络证据](../verification/fake-ip-live.json)。
- [x] 重新生成发行包，协调备份原数据并重启服务；结果见 [备份](../verification/fake-ip-backup.json)、[最终检查](../verification/final-audit.json)。

用户可保留当前模型表单，再点击“测试连接”。密钥有效性、账户模型权限和完整模型协议仍以实际连接测试结果为准。
