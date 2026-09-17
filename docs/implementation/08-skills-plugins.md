# 08 · Skills 与插件实现指导

[实现索引](README.md) · [公共契约](01-contracts.md) · 前置：[上下文](04-context.md)、[工具执行](07-tools.md)、[权限与部署](12-platform.md)

本文唯一维护 Skill 描述、激活记录、插件 manifest 及注册生命周期；目录文件结构和开发接口均为实现建议。
公共标识符使用 01 的类型，持久化使用 [02](02-storage.md) 的 repository；本文不定义物理表和权限判定算法。

## 1. 交付范围

P0 交付本地 Skill 发现、格式验证、索引、按需激活、版本快照、资源读取，以及内置/可信配置插件注册。
P0 不提供任意第三方 Python 插件的主进程动态执行，不把导入 Skill 等同于安装依赖或执行安装脚本。
P1 增加远端 Skill source；第三方可执行插件隔离进程属于后续独立工作包。
运行内核仅消费本模块产生的目录和激活内容；如何进入提示词和压缩后恢复，由 04 实现。

## 2. 建议模块

| 模块 | 实现职责 |
|---|---|
| `packages/skills/sources.py` | `SkillSource` 抽象与内置、用户、项目本地 source |
| `packages/skills/parser.py` | 安全 YAML frontmatter 解析、格式错误定位 |
| `packages/skills/registry.py` | 索引查询、候选过滤、启停与来源冲突诊断 |
| `packages/skills/snapshots.py` | 不可变内容包、hash 校验与旧版本读取 |
| `packages/skills/activation.py` | 激活/退激活、去重、加载资源记录 |
| `packages/plugin_sdk/manifest.py` | Manifest schema 与 API 兼容验证 |
| `packages/plugin_sdk/registry.py` | 原子注册、依赖拓扑、生命周期和撤销注册 |

## 3. 本模块数据契约

以下为领域 DTO；标识符和时间格式引用 01，不以 SDK 对象或本地绝对路径充当公共 ID。

```text
SkillRecord
  skill_id, source_id, revision, relative_path, name, description
  root_uri, content_hash, enabled
  scope: built_in | user | project | remote
  source_revision?, source_url?, license?, compatibility?
  trust_state: trusted | unreviewed | blocked
  validation_diagnostics[], dependency_metadata

SkillActivation
  session_id, run_id, agent_id, skill_id, content_hash
  activated_by: user | model
  reason, loaded_resource_refs[], activated_at, deactivated_at?

PluginManifest
  id, version, harness_api, entrypoint?
  contributes: {providers[], tools[], skill_sources[], skills[],
                mcp_connectors[], context_policies[], agent_templates[],
                runtimes[], ui_panels[]}
  requires: {plugins[], network[], filesystem[], credentials[]}
```

`skill_id` 由 Registry 按 source 与相对路径分配；同名但来源不同的 Skill 是不同对象。
`revision` 用于目录启停等配置并发；`content_hash` 覆盖快照清单和文件内容，`source_revision` 不替代完整性检查。
`root_uri` 由 source 解析；远端 URI 不能传给本地文件 API。
`dependency_metadata` 只描述依赖；实际授权引用 12，执行引用 07。
Manifest 的 `requires` 只表达请求，不能产生授权；不存在的扩展点必须拒绝，不能忽略后声称加载成功。

## 4. 内部调用边界

```text
SkillSource.discover(scope) -> iterable[SkillCandidate]
SkillSource.snapshot(candidate) -> ImmutableSkillBundle
SkillSource.read(snapshot_ref, resource_ref) -> ResourceContent
SkillRegistry.refresh(source_ids) -> RefreshReport
SkillRegistry.search(query, execution_context) -> list[SkillRecord]
SkillActivator.activate(skill_id, content_hash?, execution_context) -> ActivatedSkill
SkillActivator.read_resource(activation_ref, resource_ref, execution_context) -> ResourceContent
SkillActivator.deactivate(activation_ref, reason) -> ActivationReceipt
PluginRegistry.load(manifest_ref) -> RegistrationReceipt
PluginRegistry.drain(plugin_id, version) -> DrainReceipt
```

`ActivatedSkill` 返回正文、来源、快照引用和有限资源目录；不返回已执行脚本结果。
激活时需要的信任与读取判定调用 12 的策略接口；脚本调用始终交给 07 的 Tool Gateway。
运行内核不能绕过 Registry 直接扫描磁盘；Web 管理行为只通过 [10](10-api-events.md) 的服务接口。

## 5. P0 开发顺序

1. 定义配置中的 source 白名单、扫描深度、单文件/目录总字节限额；先实现本地 source。
2. 扫描标准 `SKILL.md`，排除依赖与构建目录；路径规范化和链接边界交给 12 的路径策略。
3. 使用不构造任意对象的 YAML parser；验证 `name`、`description` 及规范限制，返回具体诊断。
4. 将格式错误条目保留为不可用记录，保证一个坏文件不终止整个刷新。
5. 生成内容快照与清单，再通过 repository 原子切换目录当前版本；刷新不覆盖旧快照。
6. 按 AgentSpec allowlist、来源启用状态、信任判定筛候选；首轮仅提供名称、说明和 ID。
7. 支持用户显式选择与模型激活入口，两者执行相同的兼容和策略校验。
8. 激活后记录固定 hash；相同 run 重复激活同一快照复用记录，避免重复注入正文。
9. 为 references、assets 按需读取增加来源和 hash；scripts 只给出可审阅入口，不自动执行。
10. 向 Context Service 返回当前激活集合；压缩/恢复按固定快照重新读取，缺失则明确报错。
11. 添加启停、正文预览、版本差异、刷新诊断查询，交给 Web 管理页消费。

同名冲突必须返回候选来源供用户或配置明确选择；禁止项目 Skill 静默覆盖已信任全局 Skill。
运行中更新 source 只改变新 run 的默认版本；旧 run 保留旧快照，迁移必须形成显式记录。
来源配置的 enabled/trusted 撤销在每次激活和资源读取时直接检查当前持久配置，不等待下一次扫描。二进制资源或较大文本经 ArtifactStore 保存并返回带 MIME、大小、来源、hash 的引用；不尝试把任意 bytes 直接编码为 JSON 字符串。
如果 Skill 规则超出上下文预算，04 请求退激活或拆分任务；本模块不能仅返回截断正文。

## 6. 插件注册与更新顺序

生命周期固定为 `discover → validate → resolve_dependencies → initialize → register → ready → drain → dispose`。
`discover/validate` 只读取声明；依赖解析需要检测循环、版本冲突和缺失项，输出可审阅错误。
P0 的 `entrypoint` 只能指向构建时登记的内置 factory；可信本地配置只能选择它，不能导入任意模块。
扩展实现通过对应所有者的接口接入：模型见 03、工具见 07、MCP 见 09、上下文见 04、运行时见 05。
注册过程先在暂存目录验证名称和能力冲突，全部成功后一次切换为 ready；失败撤销本次注册资源。
将 manifest hash、实现版本与兼容范围写入锁定记录，并纳入 run 配置快照。
更新先注册新版本供新 run 使用；旧版本 drain 只拒绝新的 run 绑定，已锁定该版本的活跃 run（含等待审批者）仍可发起后续调用，当前权限撤销继续生效。待所有活跃引用结束或完成显式兼容迁移、在途操作已结束或妥善核对后才 dispose。版本引用随run快照持久化，重启后重新计算，不能只按内存调用计数释放。
dispose 失败保留诊断与资源句柄，交由进程监督器回收；不能删除已有运行需要的代码或快照。
热重载仅限静态 Skill 和无副作用配置；运行时、MCP 执行器及 checkpoint serializer 的更新走重启流程。

## 7. P1 接入点

远端 source 在确认 `io.modelcontextprotocol/skills` 扩展能力及锁版本实现后才启用，通过 [09](09-mcp.md) 获取内容。
远端记录复用 `SkillRecord`；provider URI、资源读取和归档导入由 source 处理，不引入第二套激活流程。
第三方代码插件需单独实现进程与 RPC 协议；Manifest 验证本身不构成沙箱。
UI 扩展只接受宿主授予的前端能力；不得直接读取本机文件或凭据。

## 8. 交付物与验收证据

生产组合层的声明式扩展端口接受 `skill_sources`/`skills` 的本地只读来源、`mcp_connectors` 的 ConnectionProfile、`agent_templates` 的 AgentSpec。插件来源初始为未审阅，manifest 不得设置信任或审批权限。没有实际宿主 factory 的 providers/tools/context_policies/runtimes/ui_panels 声明明确拒绝，不能只登记名称后宣称可用；任意第三方代码仍属于独立进程/RPC 接入。

声明安装与撤销在持久事务内完成；来源刷新只发布新默认。创建根/子 run 时从实际被引用的配置贡献推导插件与依赖版本锁并建立持久引用。生产 Worker 只恢复持有锁的版本；终态统一释放引用并完成 drain/dispose。重启重建引用时包含等待用户、等待子任务和待核对任务，旧版本重新登记不会覆盖用户配置变更或更新版本默认。

- 可运行的本地 source、格式诊断样本、快照清单及一次真实激活记录。
- 内置插件注册清单、冲突/回滚记录、版本锁定文件和 drain 日志。
- 对接 [S01](13-verification.md#s01)、[S02](13-verification.md#s02)；提交来源可见性、固定 hash 与策略拒绝证据。
- P1 远端 source 另附能力声明与资源来源证据，不能以本地 Skill 验收代替远端兼容验收。

## 来源与设计追溯

[原系统设计 §10 Skills](../通用Agent-Harness系统设计文档.md#s10)、[§12 插件](../通用Agent-Harness系统设计文档.md#s12)。
[专项调研：MCP 与 Agent Skills](../research/mcp-skills.md) 的 §6、§7 提供格式与来源边界；本文沿用原调研时间点，未重新核验依赖版本。
