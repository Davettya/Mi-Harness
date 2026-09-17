# 07 · 工具网关、操作账本与执行器

前置阅读：[01 公共契约](01-contracts.md)、[02 存储与一致性](02-storage.md)、[12 平台与权限](12-platform.md)。

关联模块：[05 运行内核](05-runtime.md)、[06 调度](06-scheduler.md)、[08 扩展](08-skills-plugins.md)、[09 MCP](09-mcp.md)、[13 验证](13-verification.md)。

本文实现统一工具调用路径和本机执行器。所有接口为拟议接口，P0 优先验证 Windows；本机命令执行本身不构成安全沙箱。

## 1. 唯一职责与边界

- 工具网关接收完整工具调用，执行参数校验、操作去重、授权消费、资源锁定、实际调用和结果持久化。
- 本模块唯一定义 ToolSpec、ToolResult 与执行器适配契约。
- 12 决定权限和出站规则；本模块消费决定并在接触资源前重新校验绑定条件。
- 02 提供账本、artifact 存储及事务能力；本模块定义 operation 执行和恢复算法。
- MCP 的连接、协议、认证和重连由 09 负责；MCP 工具仍通过本模块网关执行。
- Skills 与插件注册规则由 08 负责；扩展不能绕过网关直接向运行内核注入执行函数。

OperationKey、ExecutionContext 和 ArtifactRef 直接引用 01。不得按参数相同就合并两次独立用户操作。

## 2. 拟议源码位置

| 位置 | 实现范围 |
|---|---|
| `packages/tools/contracts.py` | ToolSpec、ToolResult、ExecutorProtocol |
| `packages/tools/registry.py` | 版本化注册、能力目录和实现绑定 |
| `packages/tools/gateway.py` | 唯一执行入口和授权消费 |
| `packages/tools/operations.py` | 账本驱动、结果重放和未知结果核对 |
| `packages/tools/resources.py` | 资源锁键、路径版本和调用前复查 |
| `packages/tools/executors/files.py` | 文件与文本搜索工具 |
| `packages/tools/executors/process.py` | 命令会话、输出、取消 |
| `packages/tools/executors/network.py` | 受控 search/fetch |

任务、交互、产物和检索工具分别适配其所属领域服务；不要在工具实现中复制业务状态机。

## 3. ToolSpec 与 ToolResult 的唯一契约

`ToolSpec` 是已注册实现的可调用描述，必须包含以下字段组：

| 字段组 | 要求 |
|---|---|
| `id/version/description/origin` | 稳定身份、版本、模型可见描述及 builtin/plugin/mcp 来源 |
| `input_schema/output_schema` | 输入与结构化输出校验；不把 Python 对象直接泄露到模型 |
| `effect` | read、workspace_write、external_write 或 process |
| `retry_class` | safe、idempotency_key_required 或 manual_reconcile |
| `required_capabilities` | 提交给 12 进行求交的能力要求 |
| `timeout/output_limit/concurrency_key` | 执行边界、输出上限和资源锁键生成规则 |

`ToolResult` 字段为 `operation_id/status/summary/structured_data/content_blocks/artifact_refs/truncated/exit_code/started_at/completed_at/error_code/retryable`；artifact_refs 使用公共 ArtifactRef，exit_code 在非进程工具中为空。

结果状态为 `succeeded/failed/cancelled/unknown`；`retryable` 仅是结合账本与工具实现得出的恢复提示，不能自动覆盖授权限制。

`ExecutorProtocol` 提供执行、取消和核对入口；取消或核对不适用时必须显式返回能力缺失，不能假装成功。

Host 审核后的工具元信息具有执行权威。远程 annotations、插件自报只读或模型描述不能降低 effect 或扩大 retry_class。

## 4. 第一步：注册与完整请求校验

1. 冻结 run 使用的工具目录版本，解析调用工具 ID 时不得自动切换到刚更新的实现。
2. 只接受 03 已聚合完成的 tool-call 参数；禁止边接收参数片段边执行。
3. 按 input_schema 解析和规范化，计算稳定参数摘要，解析请求的资源范围。
4. 用公共 OperationKey 查找账本；已完成操作返回已持久化结果。
5. 同键不同参数或不同实现版本返回冲突，不复用旧授权。
6. 输出模型可理解的参数错误，诊断信息只保存在受控日志中。

工具目录需要包含可用能力和原因，但不得包含密钥值、完整环境变量或未授权路径清单。

## 5. 第二步：执行账本算法

操作账本的执行状态在本模块限定为 `prepared → authorized → running → succeeded/failed/cancelled/unknown`；表字段和事务 API 在 02 定义。

1. 创建 prepared 记录，关联 OperationKey、参数摘要、实现版本、当前 attempt 和已解析资源。
2. 请求 12 决策；拒绝写入失败结论，需要审批时把耐久交互交给 05 并暂停。
3. 得到有效授权后登记授权引用，取得资源锁，重新验证参数摘要、资源身份、权限版本和授权有效期。
4. 在短事务内条件消费授权并转入 running，核对当前 fencing token；不得持有事务运行工具。
5. 执行器取得最小参数、受限凭据引用和取消句柄，输出先进入有界缓冲及产物存储。
6. 完成后先确保持久结果和大输出可读，再用 02 的事务登记账本结论与 outbox。
7. 05 将已提交的 ToolResult 转成工具消息；事件流由 10 消费 outbox，不直接广播未落盘成功。

账本中的 running 表示“可能已经产生副作用”。进程在真正调用前后崩溃都必须进入核对分支，不能因为没有结果文件就判定未执行。

## 6. 第三步：恢复与幂等

| 执行类别 | 恢复算法 |
|---|---|
| 纯读取 | 在授权仍有效且读取语义允许变化时重试；对版本敏感的读取核验资源版本 |
| 外部幂等写入 | 重用相同外部幂等键，先查询执行状态，按外部服务契约结算 |
| 本地文件写入 | 对照原 hash、目标 hash、临时文件与写入账本，识别未开始、已完成及冲突 |
| 任意命令或不支持幂等的外部写入 | 不能排除已执行则标为 unknown，交给核对流程 |

核对结论是追加记录，包含证据和后续决定。重新执行必须指明是同一操作安全续做，还是用户授权的新操作。

`ReconciliationInput` 由本模块唯一定义：`expected_revision, decision, evidence_refs[], result_ref?, note`。decision 仅允许 `confirm_completed`、`confirm_not_executed`、`accept_unresolved`；执行器核验材料、资源当前状态与结果格式后追加核对记录，不能直接覆盖旧账本证据。

- `confirm_completed` 需要足以关联本 operation 的完成证据及可重放结果；验证通过才登记已完成结果。
- `confirm_not_executed` 需要可验证的未执行证据；缺少结果本身不构成证据。允许安全续做后仍重新取得适用授权。
- `accept_unresolved` 只记录用户明确接受未知后果，不把工具标为成功或未执行；由 06 判断是否满足结束取消的条件，不能据此自动重发操作。

管理入口经 06 的 `reconcile` 调用本模块，检查当前权限与预期 revision。返回核对记录引用、结果引用和可采取动作；07 不自行修改 RunStatus。核对仍无法确定时保持未知并返回可解释原因。

同一 OperationKey 不允许两个执行器实例并行运行。资源锁保护不同 operation 对同一资源的冲突；两类锁不能互相替代。

读取已有持久结果不重复消费执行授权；实际重试执行需再次请求 12 判断并取得适用授权，不能复用已消费的一次性 grant。

## 7. 第四步：文件与搜索执行器

1. 实现 list、stat、read 和受限文本/文件名搜索，优先调用固定参数的 `rg`。
2. 使用 12 返回的路径授权解析结果，核对目标及父路径；处理 junction、reparse point、UNC 和设备路径。
3. 文件写入必须携带预期版本 hash；冲突返回当前版本信息，要求重新读取后决策。
4. write/apply_patch 先在同目录生成临时文件，验证内容和目标 hash，再通过可恢复替换流程提交。
5. 按规范化资源键获取写锁，并在锁内、真正打开资源之前复查路径和授权。
6. 高风险路径需要句柄约束或隔离执行器；无法消除路径替换竞态时明确拒绝该操作。
7. 限制单文件读取、搜索条数和总输出；大内容返回 artifact 引用与可继续读取的位置。

多文件修改不宣称具有跨文件原子性；返回逐文件结果，失败时保留可核对的变更清单。

## 8. 第五步：Windows 命令执行器

1. `exec` 创建持久命令会话，记录 run/operation/attempt 与实际进程身份，不只记录 PID。
2. 显式传入 cwd、参数数组与环境变量白名单；需要 shell 解释时使用单独且可审批的工具能力。
3. 启动时尽早把进程纳入 Job Object 或等效进程树控制；若分配失败则终止新进程并返回失败。
4. stdout/stderr 持续写入受限产物，限制总字节、单行长度、速率与磁盘占用。
5. `poll` 返回自指定偏移量之后的输出与实际会话状态，避免每次重复返回全部内容。
6. `cancel` 先请求协作退出，超时后终止受控进程树，再核验子孙进程退出。
7. Python task 被取消或 await 超时后仍须执行 OS 级回收；不能提前写入 cancelled 结论。
8. worker 异常时使用进程身份、Job Object 配置及 supervisor 配合清理；PID 重用不得误杀其他程序。

工具超时、用户取消与 worker 丢失分别记录原因。停止命令不代表撤销其已完成的文件写入或远端副作用。

## 9. 第六步：网络与领域适配工具

`fetch` 每次请求及重定向都向 12 请求目标检查，连接时校验实际目标地址，限制响应类型、体积、耗时与重定向次数。

禁止把已批准的本地模型端点变成通用 URL 代理。代理、DNS 解析和连接目标必须落在策略可核验的执行路径中。

- search 结果保留来源与信任标记，正文作为不可信内容交给 04。
- delegate/inspect_child 仅调用 06 的领域服务，不能直接创建 run 表记录。
- request_input/request_approval 交给 05 保存 interrupt 关联，工具不会自行忙等 UI 答复。
- artifact 工具调用 02 的存储接口；10/11 负责安全下载与浏览器预览。
- MCP 工具适配器返回同一 ToolResult，传输断开后按 effect 和幂等能力决定是否 unknown。

## 10. 失败处理与交付物

参数错误、权限拒绝、版本冲突、业务失败、传输失败、取消和未知副作用必须分开编码，使模型能选择修正输入、换工具、等待或请求核对。

不得把未知副作用转换成普通“可重试网络错误”，也不得仅因进程退出码为零就忽略结果校验和持久化失败。

交付物包括工具契约、注册器、网关、operation 驱动器、Windows 进程会话管理、文件/搜索/网络执行器及领域工具适配器。

验证引用 [T01–T02](13-verification.md#t01)、[R02](13-verification.md#r02)、[R06](13-verification.md#r06)、[SEC01–SEC02](13-verification.md#sec01)；进程孙级退出、账本崩溃窗口与路径竞态必须有实际证据。

原设计来源：[第 9 章](../通用Agent-Harness系统设计文档.md#s9)、第 8.6 节、第 13.3 节、第 15.3–15.4 节。

## 11. P1 MCP 持久表单等待的账本细化（2026-09-17）

现代 MCP `input_required` 已发送原始操作，因此账本仍保持 `running`，不增加新的 operation 状态。
只有 MCP 执行器已经原子保存 [09 的 protocol_continuation](09-mcp.md#mcp-continuation)
及关联 interaction 后，Tool Gateway 才允许该执行器返回 `ToolWait`。Runtime 随后通过正常 checkpoint/interrupt
进入人工等待并释放 worker 执行槽。

恢复遇到 `running` 时，只有完整的 `modern_mrtr/awaiting_input` 记录可进入执行器的 `resume_continuation`。
网关复验当前 fence、原始 OperationKey/参数 hash、工具版本、资源指纹和当前策略；不再消费原 grant，
也不重新发送最初的工具调用。用户答复不构成新增权限。其他 `running` 仍走结果核对。
续接发送前 CAS 把 continuation 标记为 `sending`；发送后失联或该状态下重启视为 `unknown`，不得重发。
结果持久化沿用原 operation_id；多轮表单用递增 round 和独立 request key，不能覆盖上一轮回答。
