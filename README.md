# Mi Harness

产品和公开助手名称统一为 **Mi Harness**。命令使用 `mi-harness`；旧 `harness` 命令与 `start-harness.ps1` 仍兼容。为保留已有会话和 API Key，数据目录 `LocalAgentHarness`、凭据库命名空间、浏览器草稿 key 与 Python 模块 `harness` 沿用原标识。详见 [命名与兼容规范](docs/implementation/20-product-name.md)。

本地 Python 服务与 React 工作台，使用 LangChain `create_agent` 作为唯一模型/工具循环，LangGraph SQLite checkpoint、独立 worker 和工具操作账本负责暂停与恢复。设计基线与实现边界见 [实施入口](docs/implementation/README.md) 和 [交付核对清单](docs/implementation/14-delivery-plan.md)。

## 安装与启动

已验证 Windows / Python 3.11；项目声明支持 Python 3.11–3.13，其他操作系统尚未做平台验收。源码开发需要 Node.js 22 与 npm。先在本目录运行：

```powershell
python -m pip install uv
uv sync --locked
npm --prefix apps/web ci
npm --prefix apps/web run build
uv run mi-harness serve --open
```

打开 `http://127.0.0.1:8767/` 后，页面会无感恢复现有会话；Cookie 不存在或已过期时，会通过同源本机接口自动建立新会话，不显示配对步骤。自动入口同时要求 Host/Origin 白名单和实际 loopback 客户端，只签发 HttpOnly/SameSite 会话 Cookie 与独立 CSRF token，匿名项目 API 仍拒绝访问。`mi-harness serve --open` 的一次性 URL fragment 与 `mi-harness pair` 继续作为兼容和故障恢复入口；默认登录有效期为 24 小时。

默认数据目录为 `%LOCALAPPDATA%\LocalAgentHarness`；数据库和产物独立于项目源文件夹。默认 Agent 使用明确标注的本地确定性 Demo，用于验证操作流程；普通文本只回显，不能代替真实模型。

初次安装完成后可直接双击 `start-mi-harness.cmd`；它只负责调用同目录的
`start-mi-harness.ps1`，启动服务并打开浏览器。命令行用户也可以继续直接运行 PowerShell 脚本。
启动失败时 CMD 窗口会保留错误信息，成功后自动关闭。`mi-harness serve` 会检查已运行实例与
端口归属，重复启动返回同一实例。

## 项目、源文件夹与会话

主页面侧栏同时展示全部项目；每个项目栏目下列出所属会话，可以折叠、展开，点击项目旁的 `＋` 立即创建会话。首次发送消息后，默认的“新会话”会自动改为消息摘要。刷新后恢复上次选择的项目和会话。

点击“添加项目”，填写名称并用“选择文件夹…”打开 Windows 原生目录窗口，可重复添加多个源文件夹，也可手动填写绝对路径。在项目旁的 `⋯` 中修改名称和文件夹；至少保留一个有效目录，相同目录在项目内自动去重。修改源文件夹前必须结束项目的运行、排队及待确认任务；移除关联不会删除磁盘文件。

第一个源文件夹是默认工作目录，相对路径以它为基准；访问其他源文件夹使用绝对路径。文件工具只能访问该项目授权的目录集合，继续拒绝目录外访问、链接/reparse point、UNC/设备路径及覆盖根目录。对话记录相互独立，但同一项目的会话共享真实文件，不创建隔离副本。

旧工作空间自动显示为单源文件夹项目，保留 ID、会话、分支、运行和产物引用；无需重新添加或搬动文件。详细契约与验收见 [项目交互升级说明](docs/implementation/16-projects-and-local-launch.md)。

## 模型与权限

在工作台打开“设置 → 模型 → 添加模型”，选择提供方、填写 API Key、选择模型名称，然后点击“保存并使用”。常见提供方自动填充地址和协议，Ollama 本地服务可免 Key，自定义兼容服务额外填写地址。完整验证通过后，10 分钟内配置不变可直接保存复用证据；快速连通性不能代替工具协议验证。新增与保存统一由服务端完整验证文本、工具、图片与流式输出。页面逐项展示通过、未检测和失败原因；图片/流式未通过时先保留原配置，重试或明确选择仅启用已验证能力后才可保存。重新验证不复用旧结果，未验证草稿不能覆盖已验证模型；已有会话需在模型菜单选择保存后的新版本。“保存草稿”不激活。

所有新模型和内置 Demo 默认使用 300K 上下文窗口；设置页只有 300K 与 1M 两档。仅在提供方文档明确说明当前模型支持 1M 时开启 1M。该选择参与验证凭证和不可变模型修订，但能力探针不会实际发送百万 token，因此 1M 仍是用户确认的配置声明。升级时，旧的当前模型配置发布一个 300K 新修订并保留历史修订与既有任务快照；原先已明确为 1M 的配置继续保持 1M。

公开助手固定为 Mi Harness。会话输入区分别选择模型与 ReAct/Plan；Plan 只读分析和保存计划，确认计划后创建新的 ReAct 任务。新默认用于新会话，已有会话保留独立偏好。运行中选择模型只影响下一次尚未绑定的模型调用，当前生成和同次重试保持原模型，界面显示待生效/实际模型。

会话输入框为连续编辑区，底栏依次提供「＋、ReAct/Plan、当前模型」，右侧圆形箭头发送；模式和模型菜单向上展开。Ctrl+V 可在当前光标处插入图片，图片/文件以带名称和删除叉号的行内卡片展示；支持上传重试、Ctrl+Z 撤销、Ctrl+Shift+Z 重做和会话草稿恢复。Enter 换行，Ctrl+Enter 发送。支持静态 PNG/JPEG/WebP（10 MiB、1600 万像素以内），带图对话需要通过视觉验证的模型。 模型一次返回多个合法工具调用时会完整保留；并行能力未验证时由运行时逐个执行，每个调用仍经过权限、审批与预算检查。

“设置 → MCP”直接编辑数据目录的 mcp.json 原文，显示文件版本和冲突；外部编辑有效后自动加载。保存不会运行 MCP 程序，启用且单独验证目录后才用于新任务。配置只接受系统凭据引用。

API Key 通过本机页面写入系统凭据库，不在配置 JSON 或对话中粘贴。编辑时留空保留原 Key，更换提供方或地址需重新输入；保存新版本不改变已在运行任务的快照。使用命令行管理凭据仍可运行：

```powershell
uv run mi-harness credential-set --target model-my-model
```

高级配置 API 中可使用返回的 `credential_ref`；普通模型页面自动完成此步骤。MCP 的 target 为 `mcp-服务ID`。该引用绑定目标，不能跨服务复用。OAuth 使用设置页授权入口，callback 由本机服务校验。

模型页的测试/保存只授权所选模型端点，不开放网页工具、MCP 或命令执行。平台默认关闭通用网络与命令能力。需要这些能力时创建用户配置 `harness-config.json`，例如：

```json
{"permissions":{"network":true,"process":true,"workspace_write":true}}
```

随后 `uv run mi-harness --config harness-config.json serve`。平台许可只是能力上界，工作区、Agent、当前策略和具体操作审批仍会继续收窄权限。修改平台配置后停止并重启。命令执行是经审批的可信本机执行，不能当作隔离沙箱；文件工具限制在选定工作区并检查链接与版本冲突。

Demo 可用示例：

```text
/tool list_files {"path":"."}
/tool request_input {"prompt":"请选择输出语言"}
/tool write_file {"path":"hello.txt","content":"你好","expected_hash":null}
/tool delegate {"goal":"检查文件目录","completion_criteria":"返回简短结果","capabilities":["file_read"]}
```

文件写入会停在审批。刷新或关闭浏览器不取消任务；待审批任务保留分支所有权并释放执行槽。副作用结果未知时进入核对，不自动重做。

## 停止、诊断与备份

```powershell
uv run mi-harness status
uv run mi-harness doctor
uv run mi-harness stop
uv run mi-harness backup C:\Backups\harness-backup-001
uv run mi-harness restore C:\Backups\harness-backup-001 C:\Harness-Restored
uv run mi-harness --data-dir C:\Harness-Restored serve
```

备份会协调排空并停止服务，成功后不自动重启。恢复要求新的空目录，验证 manifest、数据库与对象 hash。工作区外部副作用不会回滚；系统凭据库不在备份包中，换机器后需重新绑定凭据。现代 MCP 待续接的加密状态也依赖原系统凭据库。

已配对浏览器可读取 `/api/diagnostics/metrics`，查看排队、模型时延、预算、压缩、unknown 操作与恢复指标。默认关闭远程跟踪，不导出原始提示、工具参数或秘密。

## 验证与构建

Web 当前采用星见雅原服装启发的墨黑侧栏、冷白阅读区与灰青操作色，覆盖输入框、设置、菜单和状态提示。配色规范与对比度标准见 [主题规范](docs/implementation/19-miyabi-color-theme.md)，本次验证与安装包结果见 [主题验收](docs/verification/miyabi-color-theme.md)。

```powershell
uv run pytest -q --junitxml=docs/verification/latest-pytest.xml
npm --prefix apps/web test
npm --prefix apps/web run build
uv run python -m build
```

wheel 包含构建后的 Web 页面；安装 wheel 后运行 `mi-harness serve` 无需 Node。生成包之前必须构建前端。依赖固定在 `uv.lock` 与 `apps/web/package-lock.json`。

验证结果、验收矩阵与已知限制见 [交付计划](docs/implementation/14-delivery-plan.md)、[MCP 兼容记录](docs/compatibility/mcp.md) 与 `docs/verification/`。36 项自然语言任务基准在 [evals](evals/README.md)，真实供应商任务效果须单独执行，不能使用 Demo 回显作为效果成绩。

本次六项改进的规范见 [17](docs/implementation/17-improvement-contracts.md)，结果见 [验收报告](docs/verification/improvement-report.md)。统一验证入口为 `uv run python scripts/verify_all.py`。升级先停旧进程并备份：数据 schema 2 禁止旧 worker 打开，回滚需恢复升级前备份；见 [升级步骤](docs/implementation/12-platform.md)。

原系统分析保留在 [设计文档](docs/通用Agent-Harness系统设计文档.md)；P2 的 Team 编排、计划任务、多 worker 与任意第三方可执行插件属于扩展点。
