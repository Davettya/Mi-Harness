# 02 · 数据存储、Checkpoint 与产物

[实现入口](README.md) · 前置：[公共契约](01-contracts.md) · 输出供 Runtime、Scheduler、工具与 API 使用

本文件独占物理数据结构、repository 事务、checkpoint 接入、产物存储及数据备份算法。状态转换归 [06](06-scheduler.md)，操作执行账本状态归 [07](07-tools.md)，本文只存储其合法结果，不再次定义状态机。

## 1. 实现位置

```text
packages/persistence/
  migrations/       repositories/       unit_of_work.py
  checkpoint_store.py                  reconciliation_queries.py
packages/artifacts/
  object_store.py    manifest.py         retention.py
```

P0 使用 `app.db` 业务库与独立 `checkpoints.db`，后者交由锁定版本的 LangGraph saver 管理。业务 SQLite 开启外键、WAL 与可配置 busy timeout；连接按进程建立，不能跨进程共享连接对象。独立数据库意味着没有跨库原子事务，应按第 4 节处理。

Repository 不返回 ORM 活对象给图状态；返回 [01](01-contracts.md) 的可序列化引用及模块拥有的 DTO。SQL 方言差异留在本层，禁止业务模块散落 SQLite 特定语句。

## 2. 表与约束

下表是迁移时的最小列集合；通用主键、创建/更新时间与 owner/workspace 过滤需完整实现。关联数据的删除默认受约束，不配置跨会话的大范围隐式级联删除。

| 表 | 主要持久字段 | 必要约束 / 索引 |
|---|---|---|
| `workspaces` | owner、授权目录引用、policy revision | owner 索引 |
| `sessions` | workspace、title、default_agent、archived_at、revision | workspace + updated_at；元数据revision CAS |
| `branches` | session、parent、fork checkpoint、graph_thread_key、active_run_id、revision | graph_thread_key 唯一；active_run 使用条件更新 |
| `agent_specs/config_snapshots` | schema version、revision、完整无密钥快照、hash | 配置版本不可变；hash 唯一策略按对象范围 |
| `api_commands` | owner、method、规范化路由、幂等键、body hash、响应引用、到期时间 | (owner, method, route, key) 唯一；保留期由 10 的协议配置决定 |
| `runs` | branch、parent/root、input_ref、input_revision、status、snapshot、deadline、revision、next_event_seq | branch + status、root、queued 时间索引 |
| `run_attempts` | run、attempt、worker owner、lease expiry、fencing_token、结束原因 | (run, attempt) 唯一 |
| `messages` | message ID、run、branch、role、content_ref、provider metadata ref | branch + 顺序；用户输入幂等关联 |
| `model_requests` | model_attempt、run或diagnostic引用、input_view_ref或fixture引用、模型 profile、request hash、usage | model_attempt 唯一；run/diagnostic恰有一个非空；各建索引 |
| `diagnostic_requests` | DiagnosticContext引用、固定测试项、配置revision、阶段结果、报告引用 | diagnostic ID唯一；独立于run，不产生graph checkpoint |
| `context_views/context_summaries/context_heads` | 04 定义的不可变输入视图/摘要、branch当前指针、source revision | 视图ID唯一；head版本CAS；不覆盖原始messages |
| `tool_executions` | OperationKey、operation ID、tool revision、args hash、资源摘要、state、result ref | OperationKey 与 operation ID 各自唯一 |
| `operation_reconciliations` | operation ID、expected revision、操作者、07定义的核对决定、证据与结果引用、时间 | 追加保存；关联当前operation revision，禁止覆盖旧核对记录 |
| `interactions` | run、request key、interrupt ID、checkpoint ref、input revision、05定义的交互记录 | (run, request key) 唯一；绑定后对(run, interrupt ID, input revision)另建唯一约束 |
| `approvals` | interaction、ApprovalBinding、actor、decision、授权引用 | 对具体绑定检索；不因新参数覆盖旧记录 |
| `execution_grants` | 12 定义的ExecutionGrant、消费operation引用、消费时间 | grant ID唯一；剩余次数与撤销条件更新 |
| `run_waits/run_dependencies` | wait ID、parent/child、delegation operation、唤醒 revision | delegation key 唯一；parent/child 边唯一 |
| `budget_accounts/budget_reservations` | account scope为root或diagnostic、对应ID、维度、limits、reserved、consumed、reservation key | scope+ID唯一；reservation key唯一；余额条件更新 |
| `run_events` | run、seq、event ID、type、payload ref、时间 | (run, seq) 和 event ID 唯一 |
| `outbox` | event/ref、消费方、投递状态、重试时间 | 消费方 + 幂等业务键唯一 |
| `artifacts/artifact_refs` | ArtifactRef、存储 key、生产者、引用方、保留时间 | hash + 存储域；引用方索引 |
| `memories` | namespace、content/ref、source refs、status、expiry | owner/workspace/scope；检索索引与权限过滤 |
| `skills/plugins/mcp_servers/model_profiles` | 模块定义的版本配置、hash、secret refs | owner + 模块 ID + revision |

P0 可将低频配置集中到带 schema version 的配置表；但 run、operation、interaction、budget、outbox 的唯一性与并发条件不可降格为无约束 JSON 字段。P1 的 memory、child 和分支功能暂不启用时可延后相应表，基础键与迁移路径必须预留。

`CheckpointRef` 的格式见 [01](01-contracts.md)。不自行猜测或修改 LangGraph saver 的内部表结构；升级 saver 时运行其迁移流程与本项目兼容测试。

## 3. Repository 端口与事务单元

| 端口 | 保证 | 业务决策来源 |
|---|---|---|
| `RunRepository` | 幂等创建、revision CAS、读取合法 worker 记录 | [06](06-scheduler.md) |
| `BranchRepository` | 比较 expected active_run/revision 后占有或释放 | [06](06-scheduler.md) |
| `OperationRepository` | insert-or-read、参数 hash 冲突检测、结果条件提交 | [07](07-tools.md) |
| `InteractionRepository` | 去重登记、单次答复、失效标记 | [05](05-runtime.md)、[06](06-scheduler.md) |
| `GrantRepository` | 当前策略/绑定条件下消费授权；与operation变更共用事务 | [12](12-platform.md) 决策、[07](07-tools.md) 使用 |
| `ContextRepository/MemoryRepository` | 不可变版本、active pointer CAS、记忆检索权限谓词 | [04](04-context.md) |
| `BudgetRepository` | 原子预留/结算/释放，根级余额保护 | [06](06-scheduler.md) |
| `EventRepository/OutboxRepository` | 同事务登记耐久事件与投递意图 | [10](10-api-events.md) 定义内容 |
| `ArtifactStore` | 完整对象先落盘，引用后发布，范围读取 | 本文 |
| `CheckpointStore` | 调用 saver，读取状态与持久引用，生命周期关闭 | [05](05-runtime.md) 决定执行模式 |
| `RunViewRepository` | 一次数据库读快照内投影消息、工具、开放交互与耐久last_seq | [10](10-api-events.md) 定义投影shape |

管理与读取端口明确为：`RunViewRepository.read_snapshot`、`EventRepository.read_after/retained_range`、`SessionRepository.patch`、`ApiIdempotencyRepository.reserve/read/complete`、`ArtifactStore.put_stream`、`MemoryRepository.list/get/compare_and_set`。API幂等保留记录使用api_commands表；其完成回执与业务写入一起提交。Memory修改后的索引/缓存失效意图登记outbox，读取在索引更新前仍按主记录revision和可见状态过滤。

`UnitOfWork` 提供短事务边界。一次业务事务可共同包含 run 更新、预算预留、dependency、interaction、event/outbox。不得在数据库事务内等待模型、HTTP、用户审批或外部命令。

授权消费与 operation 转入 running 必须共用该事务；任何一个条件更新失败则一起回滚。交互在 interrupt ID 尚未产生时使用稳定 request key 去重，不能依赖 SQLite 对NULL的唯一性检查。具体准备键由 05 提供。

UI snapshot 使用业务库一致读事务，读出的记录与 `last_seq` 来自同一快照；耐久业务变化与对应event应在同一写事务登记。事件序列与 outbox 是同库事实，不能把“已广播到浏览器”作为提交前提。大对象只取已提交引用，不能为了读取完整文件长期持有读事务。

snapshot 不混入独立checkpoint库的“最新状态”补齐字段，避免两个时间点混合；checkpoint进度先由合法worker形成业务投影，再进入该读取边界。

SQLite CAS 使用带期望 revision/状态/owner 的条件 UPDATE，受影响行数为 0 表示冲突而非成功。claim 涉及读取后更新时使用适当写事务；冲突仅重试这个短事务，不重发对应外部操作。

所有 worker 产生的领域状态提交携带 fencing token 并由 repository 检查。它阻止过期 worker 提交数据库状态；对旧进程仍可能存在的外部副作用，按 [06](06-scheduler.md) 的接管规则处理。

## 4. 业务数据与图状态的协调

实现三个明确的持久化边界，不能用一个“保存成功”掩盖它们：

1. **输入受理边界**：用户输入、run、输入幂等键及受理事件处于同一 app.db 事务。API 成功受理以此为准。
2. **图边界**：Runtime 请求 checkpoint 同步持久化；工具执行前是否已跨过边界由 [05](05-runtime.md) 强制。CheckpointStore 不将 SDK 内存接收当作磁盘提交完成。
3. **工具结果边界**：已完成产物和执行结果在 ledger 中形成稳定引用。之后图持久化失败仍可读取既有结果；具体重放分支见 [07](07-tools.md)。

`checkpoint_links` 或等价索引记录 graph_thread_key、checkpoint ID、run_id、input_revision、runtime revision 与观察时间。索引更新可晚于 saver 提交，恢复查询应能从 checkpoint 元数据重建，不能靠这个索引声称两个数据库原子提交。

维护扫描查询：有终态图而 run 未结算、ledger 已完成而结果事件缺失、待处理 outbox、已完成 child 对应未唤醒 wait、产物引用缺失对象。查询返回候选交由对应模块处理，不由存储层自行重跑工具或更改调度状态。

对象文件存在但数据库事务未提交属于孤儿对象，先保留并等待 GC；数据库引用指向缺失对象属于数据损坏，停止相关恢复并报告。不得用“空字符串结果”掩盖损坏。

## 5. ArtifactStore 实现步骤

1. 为 owner/workspace 创建受控存储域及临时区，限制配额和单对象大小。
2. 以流方式写临时文件并累计 hash/size；超过限额即终止，不先全部载入内存。
3. flush 并按平台支持完成持久化，同文件系统原子发布对象；异常时保留可诊断状态。不要承诺跨文件系统 rename 原子。
4. 验证对象存在、hash 与大小，再提交 artifacts 元数据与引用。
5. `read_range`/download 始终先验证逻辑产物归属，不能直接把 URL 参数拼为宿主路径。
6. 预览只返回元数据与受控内容；HTML/SVG 安全呈现归 [11](11-web.md)，访问及出站策略归 [12](12-platform.md)。

文件输出到用户工作区是工具副作用，不是 ArtifactStore 内部对象发布；写入算法见 [07](07-tools.md)。工作区文件可以登记来源与 hash，但不能因此把任意宿主路径变成可下载产物。

## 6. 迁移、备份、恢复与保留

迁移记录 schema version、应用版本、checkpoint saver 版本。启动时检测未来版本，不兼容则进入只读诊断，不自动逆向改写。

P0 备份采用维护窗口：由 [12](12-platform.md) 停止新写入、暂停/结束 worker 并确认写者释放；本层分别执行 SQLite backup API，保存对象引用 manifest、配置快照与完整性摘要。期间冻结 GC。不能仅复制 WAL 模式下活跃数据库的主文件。

恢复到新数据目录，校验 manifest、外键、产物 hash、版本兼容和 checkpoint 可读性；验证通过后再由 supervisor 切换活动目录。密钥恢复归平台层。备份不包含用户工作区的隐式回滚能力。

GC 根据 sessions、branches、checkpoints、archives 与 backup manifests 的可达引用计算候选，考虑保留期，再执行经授权的删除。长期记忆删除同时处理其索引与缓存，历史备份的保留规则另行告知。

PostgreSQL 迁移在需求出现后实施：保持业务端口及唯一性语义，替换 claim/CAS SQL 和 checkpoint saver；验证并发、序列与数据导入后切换。P0 不为未启用的多人部署提前维护两套活跃数据库。

## 7. 开发顺序与交付物

先做 schema migration 与基础 repository，再做 UnitOfWork/CAS/约束测试；随后接 ArtifactStore、checkpoint 生命周期和协调查询；最后做备份恢复与 GC 预览。

交付迁移文件、repository 实现、事务边界说明、恢复诊断命令所需数据、可导入的脱敏样例数据库。验收定义只在 [DB01](13-verification.md#db01)、[DB02](13-verification.md#db02)、[D01](13-verification.md#d01) 维护；副作用恢复的联调引用 [R02/R03](13-verification.md#r02)。

设计来源：[原设计 §13](../通用Agent-Harness系统设计文档.md#s13)、[§6](../通用Agent-Harness系统设计文档.md#s6)。
