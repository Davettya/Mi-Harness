# Web 与 API 验收记录

日期：2026-09-17；平台：Windows；浏览器：Codex In-app Browser。测试连接真实 FastAPI、SQLite、Scheduler Worker 与 LangGraph runtime，使用标注的确定性 `demo` 模型，无真实供应商调用。所有写入位于专用临时数据目录 `%TEMP%/harness-web-smoke-20260917` 和其 `workspace/` 下。

## 初始自动化与构建（模型设置修订前）

| 检查 | 本次结果 |
|---|---|
| `npm ci --no-audit --no-fund` | 使用锁文件安装通过，127 packages |
| FastAPI OpenAPI 导出与 `npm run generate:api` | 通过；含实际 PluginManifest、ContextPinInput 与全部路由 |
| `npm test` | 3 个文件、11 项通过 |
| `npm run build` | TypeScript 与 Vite 通过，生产包约 249 kB JS、18 kB CSS |
| `pytest tests/test_api.py -q` | 9 项通过；仅依赖 TestClient 的 anyio alias 弃用警告 |

前端测试验证 SSE 分片解析、重复/缺口/错误 run、snapshot 恢复、临时 chunk 去重、committed 替换、断线保留中断说明、旧 revision 拒绝，以及网络模糊重试保留同一幂等键和正文。API 测试验证一次性/过期配对、登出、独立 Host/Origin/CSRF、幂等重放/正文冲突、上传归属/大小/去重、主动内容响应头、错误不回显秘密、游标预检查、禁用能力、静态插件类型与路由以及空 assistant 工具调用投影。

## 真实浏览器旅程

| 功能 | 操作与观察结果 |
|---|---|
| 首次配对 | CLI 发行的一次性 ticket 从表单兑换；会话 cookie 跨 API 重启保持有效 |
| 工作区与会话 | 创建临时工作区；创建任务会话；会话重命名为“ Harness 全流程浏览器冒烟 ”；归档与取消归档均根据服务端回执刷新列表 |
| 普通任务 | 中文目标提交后 queued → completed；回答明确标记确定性演示 |
| 审批与重连 | `write_file` 停在开放审批；刷新浏览器和重启 API 后仍可读取完整目标、内容、diff、绑定与有效期；选“仅允许本次操作”后 resumed → completed |
| 文件副作用与工具面板 | `ui-smoke.txt` 实际写入一次，内容“UI 审批成功，执行一次。”；工具 succeeded，33 字节，产物 SHA-256 `d54b3e09491196b839996a9aa95b5c3aca31b047eba3906c7ab6c4299ee96f54` |
| 产物纯文本预览 | 通过真实 artifact_id 读取并显示上述中文内容；受控下载链接可用 |
| 附件提交 | 浏览器文件选择器上传测试文件；取得引用后显示附件 chip；附件随任务提交，任务完成 |
| 模型设置 | 打开 demo 的实际 ModelProfile 字段；审阅并保存 revision 1 → 2；固定能力诊断返回，不宣称真实供应商通过 |
| Skill 来源与正文 | 创建 `smoke-skills` 来源，刷新发现 `smoke-notes`；读取固定 hash 正文；停用 revision 1 → 2，启用 → 3 |
| 上下文固定内容 | 写入“保持中文输出，文件写入必须按审批范围执行”，pins revision 0 → 1；后续回合仍可见 |
| 上下文预算 | 最终构建显示估算峰值 11,417、对应输入预算 30,000、算法 `utf8-conservative-v1`；明细保留来源和用量未知项 |
| 记忆全流程 | `memory_propose` 产生 proposed v1；详情审阅接受 → v2；编辑内容 → v3；明确展示内容和 workspace 范围后遗忘；服务端确认后列表移除 |
| 分支 | 已保存 checkpoint 创建 `preserve_external` 分支；回执明确外部副作用不回滚；新分支任务可完成 |
| 补充输入 | `request_input` → waiting_user；填写“中文”并提交，回执后恢复并完成，结果包含输入 |
| Steering | 专用测试 Worker 暂停期间提交 queued 任务，再通过独立入口提交补充指令；Worker 恢复后仅出现一次 steering 人类输入并完成，无额外排队 run |
| 父子任务 | `delegate` 创建子任务，父任务 `wait_children` 等待恢复并完成；任务树显示子目标/状态/预算；点击子任务看到独立结果 |
| 取消 | `request_input` 等待中取消，服务端返回 cancelled；最终构建停用已结束任务的旧答复入口，刷新后开放交互消失 |
| 静态插件 | 审阅并导入 `ui-smoke-static@1.0.0` 的 Skill 来源声明，ready revision 1；排空后 disposed revision 2，显示实际生命周期回执 |
| HTML 隔离 | 创建 `safe-preview.html`，脚本试图将段落改成 `SCRIPT_EXECUTED`；iframe 中仍显示“脚本执行前”，无脚本执行；artifact `03dc5708-aaa8-411a-a457-4d372beb12a7` |
| 健康与诊断 | 页面读取 health 与 metrics：api_alive/worker_ready/db_writable 均 true；10 runs 中 9 completed、1 cancelled；诊断不含原始内容 |
| 窄屏与截图 | 在 390×844 检查会话导航抽屉、重命名对话框、输入与操作可达；随后恢复默认 1280×720。工作台、任务树和隔离预览截图已通过浏览器工具输出并目视检查 |

## 验收中修正

修正 assistant 工具调用空文本导致快照 500、上传引用混入显示字段、设置分类切换保留旧 ID、审批审阅材料隐藏、产物投影 ID、文本 iframe 无显示、配置/记忆字段与真实领域 DTO 不符、记忆来源类型、steering 不适用状态仍可操作、取消旧答复卡仍可提交、当前任务选择器状态滞后、分支选择与当前 run 不一致、长历史仅截取无法继续查看、窄屏关闭导航仍进入键盘遍历等问题。后端领域问题由所属模块修复，前端不猜测补全服务器事实。

## 尚未由真实外部服务验证的范围

真实 OpenAI/Anthropic/其他供应商的在线兼容性和费用、第三方 MCP OAuth 服务及 elicitation 真端点未在这次浏览器旅程连接；相应 UI/协议实现与独立后端 fixture 测试不应被描述为外部供应商在线通过。未知副作用核对面板已接真实接口和证据上传，本次浏览器未注入一次真实外部工具不确定结果；该场景依据后端故障注入验收记录。这里不把源码存在或静态按钮当作端到端结果。

## 2026-09-17：简化模型设置追加验收

本次将模型页替换为独立 `ModelSettings`：普通提供方只有“提供方 / API Key / 模型名称”三项，模型名称使用原生可编辑下拉目录，底部“取消 / 保存并使用”。旧配置可从列表编辑，已保存密钥留空保留且永不回显；切换提供方清除密钥、模型名称、发现结果和测试结果；仅自定义服务显示 URL，Ollama 不显示密钥输入。列表显示提供方、模型和当前使用状态，其余设置分类保留。旧非官方地址或未知提供方按自定义服务保守迁移，明确要求重新输入原服务密钥。

验证使用专用内存 UI fixture `scripts/model_setup_smoke_fixture.py`，服务不读取用户 Harness 数据、不调用真实供应商、不访问系统凭据库；输入均为虚构测试数据。浏览器为 Edge。最终 fixture 地址使用 `http://localhost:8879`，与用户服务 `127.0.0.1:8767` 分离 Cookie 主机。早期曾使用相同 127.0.0.1 主机，fixture 改写了非 HttpOnly CSRF Cookie（未改会话 Cookie），已通知总验收执行者重新配对用户服务恢复；不将该意外当作会话隔离已通过。

| 检查 | 本次结果 |
|---|---|
| 旧配置编辑 | 浏览器 API Key 字段为空、type=password；留空测试与保存请求均带 profile_id、不含 api_key |
| 保存并使用 | fixture 返回后列表当前标记更新，敏感草稿清空，成功说明明确只影响后续任务 |
| 提供方切换 | 输入虚构密钥并完成测试后切换 Anthropic，密钥和模型为空，旧测试结果消失 |
| 目录发现与自定义 ID | 仅点击“刷新模型列表”发送 discover；保留自定义模型文本，显示发现的模型数量 |
| 自定义 / Ollama | custom 显示 URL；切换 Ollama 后 URL 与密钥输入消失；无 api_key 完成测试和保存 |
| 错误脱敏 | fixture 错误正文故意包含虚构密钥标记；页面仅显示固定安全错误说明，不显示原始正文 |
| CAS 冲突 | 服务端返回 REVISION_CONFLICT；用户草稿保留，显示最新模型摘要与“载入最新设置”；取消恢复空表单 |
| 请求证据 | 8 次显式请求均有 CSRF；仅 3 次 save 带独立 Idempotency-Key，test/discover 无幂等命令缓存；证据只记录密钥是否存在，不记录值 |
| 自动化 | `npm test` 4 文件、20 项通过；新增旧密钥留空、提供方隔离、Ollama、自定义 URL、旧代理保守迁移、敏感错误和 MODEL_PROFILE_PINNED 等回归覆盖 |
| 构建与视觉 | TypeScript/Vite 完整构建通过；Edge 检查表单、列表、输入与底部操作布局。最终去掉原生输入框重复的装饰下拉箭头，格式化与 20 项测试再次通过；最终发布构建由总验收执行 |
| 清理 | 专用 fixture 进程和测试标签页关闭；未重启 8767 用户服务 |

以上浏览器测试验证界面与传输行为，不代表真实提供方连通、真实密钥落库或实际模型收费行为已在线测试。生产服务保存/验证/default Agent 切换的结果由独立后端验收记录覆盖。

## 模型设置修订：实际服务重启后复验

北京时间 21:52 完成原用户数据目录的协调备份与重启，入口 `http://127.0.0.1:8767/`。Codex In-app Browser 的原配对正常读取新页面与 8 类提供方目录；腾讯云 Token Plan 仅显示提供方、API Key、模型名称；切换自定义服务出现地址，切换 Ollama 后地址和密钥输入消失。恢复腾讯云表单并目视检查布局，保留页面供用户配置。此旅程未输入凭据、未保存虚构模型，默认仍为原 Demo；此前 Edge fixture 的 Cookie 不参与此次 IAB 验收。当前 API/Worker/supervisor 就绪且数据库完整性正常。
