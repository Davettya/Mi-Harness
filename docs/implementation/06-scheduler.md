# 06 · 任务调度、预算与子 Agent

前置阅读：[01 公共契约](01-contracts.md)、[02 存储与一致性](02-storage.md)、[05 运行内核](05-runtime.md)。

关联模块：[07 工具执行](07-tools.md)、[12 平台与权限](12-platform.md)、[13 验证](13-verification.md)。

本文是待实施指南，不代表已有队列、恢复或多 Agent 能力。P0 实现单用户、本地 SQLite、单 worker 进程及有限异步并发；父子 Agent 为 P1。

## 1. 唯一职责与边界

调度器决定哪个 run 可以执行、由哪个 attempt 执行、何时释放执行槽，以及等待或故障后是否允许再次调度。

- 拥有 RunStatus 的合法转移、branch 逻辑所有权、lease 与 fencing 语义。
- 拥有根任务预算预留与结算、取消树、子任务派生和等待唤醒协议。
- 调用 RuntimeAdapter，接收 RuntimeOutcome；不定义图节点、消息压缩或审批界面。
- 使用 02 的仓储和事务设施；不在本模块创建表、实现迁移或自建 checkpoint saver。
- 工具副作用的确定性由 07 报告；调度器不能用 lease 到期推断副作用可重试。

RunStatus、ExecutionContext、RuntimeOutcome、标识符和事件封装只使用 01 的定义。下文只说明调度动作及前置条件。

## 2. 拟议源码位置

| 位置 | 本文负责的实现 |
|---|---|
| `packages/scheduler/service.py` | 提交、取消、恢复命令与状态转移检查 |
| `packages/scheduler/worker.py` | 领取、续租、调用 runtime、释放执行槽 |
| `packages/scheduler/ownership.py` | branch 逻辑所有权和安全接管判断 |
| `packages/scheduler/budget.py` | 原子预留、用量结算、额度回收 |
| `packages/scheduler/recovery.py` | 租约检查、孤儿 attempt 与恢复协调 |
| `packages/scheduler/delegation.py` | P1 子任务派生、依赖和结果聚合 |
| `packages/scheduler/waits.py` | P1 等待登记、唤醒去重和补偿 |

仓储接口由 02 实现。API 只调用本模块服务；禁止接口处理函数直接修改 run 状态。

对外端口固定为 `submit`、`respond`、`cancel`、`reconcile` 和 P1 的 `create_branch/steer`。`respond` 单次保存匹配答复，在当前checkpoint的恢复条件满足后重新入队；`reconcile` 接收 07 的核对输入并在其核验结果后执行合法状态转换，不把核对当作普通审批答复；`create_branch` 要求可恢复 checkpoint，交由 02 建立新的独立执行键；`steer` 使用独立输入命令并仅在 05 的安全边界消费，不同时创建普通排队 run。

`SchedulerConfig` 集中保存全局模型并发、worker槽位、工具分类并发、每root child并发、派生深度、lease/heartbeat/扫描周期。初始模型并发4、每root child并发2、派生深度2，其余值通过PoC确定；不能在模型或工具模块另设不受Scheduler管理的全局配额。

`BudgetPolicy` 的限额字段为 `max_model_calls/max_total_tokens/max_cost/max_wall_seconds/max_tool_calls/max_children`；费用同时声明币种和估计策略，未知价格不以零计。AgentSpec仅引用或填写该结构；累计计数由预算服务持有。

## 3. P0 第一步：集中实现状态转移

1. 以 01 的状态集合建立 `transition(run, action, evidence)` 领域函数。
2. 每个动作要求预期修订号；同一 action 的重放返回已有结果，竞争更新返回冲突。
3. 将状态、修订号、branch 所有权变化和 outbox 写入同一业务事务。
4. `claim` 仅处理可领取任务；获得 branch 所有权与创建 attempt 必须原子完成。
5. 暂停等待时保留 branch 所有权，释放 worker 槽、模型槽及未使用的临时资源。
6. 收到有效交互答复后保存至恢复屏障；满足该checkpoint所需条件才重新入队，05 构造恢复输入。
7. 进入终态前核实结果登记、必要产物持久化和未完成工作处置情况。
8. 终态释放 branch 所有权，并按提交顺序使后继 run 具备领取资格。

同一 branch 的新用户消息在 P0 形成排队 run。取消与审批答复走独立服务动作；不得作为普通待处理消息等待模型理解。

| 动作 | 合法状态转移及必要条件 |
|---|---|
| 领取 | queued → running；branch 可占有或仍由自身占有，配额允许 |
| 等待 | running → waiting_user / waiting_children；交互或 wait 已耐久，checkpoint 已关联 |
| 唤醒 | waiting_user / waiting_children → queued；答复有效或等待条件满足，仅推进一次 |
| 失联 | running → recovering；attempt 超时或确认失联 |
| 安全接管 | recovering → queued；旧执行者退出且副作用已核对 |
| 人工核对 | recovering / running → needs_review；工具返回未知副作用或一致性无法自动确认 |
| 核对完成 | needs_review → queued；核对记录允许续跑且恢复输入已准备 |
| 正常结束 | running → completed / failed；持久结果、资源与子任务均满足终态条件 |
| 取消未执行任务 | queued / waiting_user → cancelled；不存在仍执行的操作或未结子任务 |
| 请求停止 | running / waiting_user / waiting_children / recovering / needs_review → cancelling；取消屏障已登记 |
| 停止完成 | cancelling → cancelled；执行者退出，未结副作用已有明确处置记录 |

表外转移默认拒绝。取消期间仍发现未知副作用时保留 cancelling 和核对标记；只有用户确认结果或明确接受其未决状态后，才可按审计记录结束取消。

**多个interrupt采用收集后统一恢复。** Worker在checkpoint绑定时固定该次恢复所需的interrupt集合；`respond`保存单项答复但不立即启动图，齐备后仅一次入队并按ID构造完整resume映射。部分答复仍保持等待，过期/拒绝的处理遵循05交互语义；取消不受此屏障限制。P1混合用户/child等待也按同一checkpoint聚合就绪条件，用户等待优先显示，内部child就绪只更新屏障，不抢先恢复；需人工核对的结果优先进入needs_review。

屏障收集的是 05 的 InterruptResolution，不限于用户回答；到期扫描与 respond 共用 revision CAS，恰有一个结论获胜。过期项按 05 生成超时 resolution 后参与齐备判断；已登记取消屏障时禁止唤醒。

## 4. P0 第二步：实现领取与 lease

领取事务必须同时验证排队资格、branch 所有权和并发额度，使用条件更新避免两个 attempt 同时成功。

持久化字段及索引见 02；调度器负责生成新的 fencing token，并把该 token 传入当前 ExecutionContext。

- 心跳续租只接受当前 attempt 和 fencing token；续租失败立即停止发起新的模型、工具或派生请求。
- 运行结果、用量结算和状态推进均核对当前 token；过期 attempt 的写入被拒绝并记录审计事件。
- 内存 semaphore 仅控制当前进程容量，不代替数据库领取条件。
- worker 异常退出时由恢复扫描器发现超时 attempt；启动时也运行一次相同扫描。
- 不在持有数据库写事务时等待模型、工具、网络或 OS 进程结束。

P0 先固定一个 worker 进程，仍实现 fencing，避免重启窗口中旧 worker 与新 worker 同时提交。

## 5. P0 第三步：安全恢复与完成判定

恢复扫描器把失联 attempt 交给恢复协调流程，不直接重新排队。

1. 从 supervisor 获取旧 worker 身份与退出证据，从 07 获取其进程树和未结操作情况。
2. 撤销仍有效的执行授权，停止可控子进程，并等待确认；仅“已发送停止”不算已退出。
3. 请求 07 分类操作账本：可重试、已完成、需外部核对、结果未知。
4. 使用 02 提供的一致性检查，确认 checkpoint、结果记录和业务状态可关联。
5. 全部满足安全重入条件后生成新 attempt；存在未知副作用则保留 branch 所有权并进入核对流程。
6. 核对结论必须持久化，指明操作者、被核对 operation、结论和允许采取的后续动作。

运行内核报告最终文本只构成完成候选。调度器需确认 artifact 可读取、最终结果已登记、所有命令已退出，以及子任务已结算或按明确协议转交。

## 6. P0 第四步：取消与槽位管理

`Scheduler.cancel` 首先持久化取消意图，再调用RuntimeAdapter.request_cancel通知当前 attempt。重复请求返回同一取消进度。

- 未启动的排队任务可直接进入取消终态，并释放预算预留；仅在自身持有时释放 branch 所有权。
- 正在执行的任务进入取消流程；05 停止模型与图推进，07 停止具体 OS 进程或传输。
- 等待中的任务无需领取普通执行槽即可接受取消；取消不能依赖用户审批完成。
- 执行器无法确认副作用结束时保留取消中状态和核对标记，UI 通过 10 读取实际状态。
- 物理执行槽释放与逻辑所有权释放是不同操作，必须分别记录原因。

worker 空闲槽由有界 semaphore 管理；模型与工具各有独立限流器。等待用户或孩子时不得继续占用模型连接。

## 7. P0 第五步：预算账本

预算至少覆盖 token、模型费用、执行时长和工具次数；不支持费用准确计量的 provider 使用显式估算并记录不确定性。

模块私有的 `BudgetReservation` 表示一次已批准但未完全消费的额度；持久化结构由 02 定义。

1. 执行模型或产生独立成本的动作前，按 root 预算原子预留上界。
2. 03 返回实际或估算 usage 后，按稳定结算标识记录实际消费并释放差额。
3. 摘要、重试和后代消费全部归入同一 root；不得新建无关联 root 绕过限制。
4. 预算不足在调用前返回可解释拒绝；不得发起调用后再把超额视为普通日志。
5. 调用结果未知时冻结相应预留，待用量核对后结算；不能把全部预留直接退回。
6. 新配置适用于后续快照；活跃任务扩额要求新的显式授权，已消费金额和既有账本记录不可覆盖修改。

P0 先闭合单 run 的预留与结算，P1 在相同账本上增加子任务额度划拨，避免维护两份成本权威来源。

独立诊断按 01 DiagnosticContext 建有硬限额的 diagnostic 预算账户，复用本预算服务和全局模型许可，不占用 Agent worker/branch，不从不存在的 root 取额度。诊断次数与 token/费用受账户及 owner 速率限制；超时或客户端失联后回收受控探针资源，未知用量仍待核对。它不能委派或执行工具；真实工具测试由普通测试 run 承担。

## 8. P1：幂等委派与独立 child run

`delegate(parent, OperationKey, request)` 接收稳定操作键和已授权的子任务请求，返回已有或新建 child 引用。

模块私有 `DelegationRequest` 包含任务目标、完成标准、资料引用、结果约束、deadline、预算上限与权限收缩请求。

1. 校验 parent 未取消、深度和并发上限、依赖无环，以及 12 返回的子权限不扩大结论。
2. 在一个业务事务内创建 child、独立内部 branch/thread 关联、依赖边、预算预留与 outbox。
3. 相同 OperationKey 重放返回同一个 child；参数摘要变化返回冲突。
4. child 默认只接收任务说明和选择的材料引用，04 负责构造其上下文。
5. child 使用独立逻辑 branch 所有权；不占用或继承父 branch 的写锁。
6. child 完成后保存规范结果引用和消费结算，父 Agent 再核对证据与整合结论。

独立 worktree 和写资源协调通过 07 的工具能力执行；调度器不会把 worktree 当作 OS 权限隔离。

## 9. P1：耐久等待与唤醒

模块私有 `ChildWaitSpec` 描述需要等待的 child 集合、全部/任一等完成条件及失败处理约定，不重新定义公共 interrupt schema。

1. 调度器登记 wait 标识及等待条件，05 用类型化内部 interrupt 保存对应 checkpoint 并退出图调用。
2. wait 从登记到 checkpoint 绑定期间保持未就绪；只有二者均耐久后才能唤醒父任务。
3. wait 绑定时再次查询 child 状态，覆盖“child 先完成，父任务后进入等待”的竞态。
4. child 结算写唤醒 outbox；消费者以 wait、interrupt、checkpoint 关联进行条件推进。
5. 事件重复投递、消费者重启或多次补偿都只能使父任务入队一次。
6. 定期 reconciliation 重新计算未完成等待条件，修复通知丢失或登记中断。
7. 内部等待不得进入用户审批列表，父任务等待时释放物理执行槽。

取消树先持久化 root 取消屏障，再标记全部后代；委派事务检查该屏障，杜绝取消后继续派生。

## 10. 失败分支与交付物

| 故障 | 本模块必须采取的动作 |
|---|---|
| branch 已被等待中的 run 占有 | 保持新 run 排队，不夺取所有权 |
| lease 过期但旧进程仍存活 | 暂停接管，协调停止与副作用核对 |
| 旧 attempt 提交结果 | fencing 拒绝，保留拒绝证据 |
| child 失败或取消 | 按已登记等待条件唤醒父任务，结果明确保留失败 |
| 取消期间出现外部结果未知 | 保留核对状态，不宣称操作已撤销 |
| 预算结算重复 | 返回原结算结果，不重复计费或退款 |

交付物包括调度领域服务、worker 入口、恢复扫描器、预算服务、P1 委派与等待协调器，以及状态转移和恢复策略说明。

验证引用 [A01–A04](13-verification.md#a01)、[R02](13-verification.md#r02)、[R06](13-verification.md#r06)；其中 P1 用例在 P0 明确标为未实施，不能计为通过。

原设计来源：[第 8 章、第 13 章与第 15.6 节](../通用Agent-Harness系统设计文档.md#s8)。运行内核的暂停机制按 [05](05-runtime.md) 实施。

### 2026-09-17 分支发布边界

分支创建先校验源 checkpoint 与分支 revision，再将 working history 复制到新的 graph thread；复制成功后短事务重新校验源 revision 并发布业务分支。复制失败不得留下 UI 可用的空分支。发布时复制源分支当前固定约束为独立记录，后续编辑互不影响；外部文件与服务状态继续保持原状。若复制成功但业务发布失败，未被引用的 graph thread 只作为可诊断孤儿保留，不能自动宣称分支创建成功。
