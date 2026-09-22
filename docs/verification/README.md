# 本地验收报告

2026-09-19 侧栏与生命周期增量：会话归档状态与列表视图已拆分，归档后新建不再复现旧会话；桌面侧栏支持持久化拖动/键盘调宽；项目可在 Harness 内软移除且不触碰本地文件。全量后端 203 项通过、1 项跳过，Web 47 项通过，发行构建、重启和当前实例临时项目验收通过。证据见 [侧栏、会话归档与项目软移除验收](sidebar-session-project-lifecycle.md)。

2026-09-19 工具消息展示增量：对话中的工具消息默认折叠且不显示工具头像，鼠标与键盘均可展开；Web 42 项、后端 194 项通过，1 项因 Windows 无特权符号链接权限跳过。生产 Web、sdist/wheel、隔离安装及当前服务新资源读取均通过；契约、资源指纹与边界见 [工具消息展示验收](tool-message-presentation.md)。

2026-09-19 已将 `skill-creator` 导入项目本地 `skills/` source；静态格式校验和一次真实 `skill_activate` 调用均通过，固定哈希、运行上下文与返回正文已回读核对。证据见 [`skill-creator` 导入与调用验收](skill-creator-import.md)。

2026-09-19 产品名称统一为 **Mi Harness**，页面、API、命令行、安装包和文档同步更新；范围、兼容标识与执行证据见 [产品命名验收](product-name.md)。

2026-09-19 配色增量：星见雅原服装启发的墨黑/冷白/灰青主题已完成，40 项 Web 回归、12 项相关后端回归与浏览器配色检查通过；本次构建和检查范围见 [主题验收](miyabi-color-theme.md)。

最新增量：2026-09-18 已将普通打开页面的手动配对改为同源 loopback 无感会话。最终统一验证 **194 项通过、1 项跳过**，Web **40 项通过**，生产构建、sdist/wheel、隔离安装运行和原数据目录重启后的空 Cookie 现场验收全部通过。原因、安全边界和证据见 [无感配对验收](seamless-local-pairing.md)。下文保留较早阶段的历史记录。

日期：2026-09-17。对象：Mi Harness 0.1.0 的 P0/P1 本地实现，Windows / Python 3.11.9。用户已指定“先完成本地验收，真实服务稍后配置”。

当前记录包含模型设置简化修订。后端全量首次运行 **110 项：109 通过、1 失败**，失败为 Windows 恢复后启动时状态文件被短暂占用；修复有界读取重试后，平台/配置/发行恢复 **10 项全部通过**（含新增故障注入）。两个执行批次合并覆盖 **111 个不同用例，无未解决失败**，不是一次全量 111 项运行。前端 **20 项通过**。独立环境安装最新 wheel、启动 API/Worker、完成真实 graph 任务及停止通过；当前用户数据备份后已重启，并检查实际模型页。

最初 99 项后端、11 项前端结果保留于 [初始归档](initial-20260917/verification-result.json)。测试连接实际 LangChain/LangGraph/SQLite 与独立 Worker；模型回复使用明确标记的 Demo，provider/MCP 协议由本地可控服务验证。

| 证据 | 内容 |
|---|---|
| [功能实现核对清单](../implementation/14-delivery-plan.md) | 逐模块实现、设计决定、验收编号与范围限制 |
| [全量执行记录](model-settings-full/verification-result.json) / [日志](model-settings-full/pytest.log) / [JUnit](model-settings-full/latest-pytest.xml) | 保留首次 109 通过、1 失败与当时源码指纹；编译、正确性检查与构建通过 |
| [修复回归日志](platform-retest.log) / [JUnit](platform-retest.xml) | 10 项通过，包括真实备份恢复后重新启动和状态文件瞬时/持续读取故障 |
| [源码与依赖指纹](source-manifest.json) | 被测源码及 fixture 的 SHA256、依赖锁、OS/Python/硬件；无 Git 仓库故不提供 commit ID |
| [Web 验收](../../apps/web/VERIFICATION.md) | 前端自动化、实际浏览器操作与观察、390px 窄屏 |
| [wheel 验收](wheel-verification.json) | 独立安装、内嵌资源、已安装程序的启动/任务/停止 |
| [无感配对验收](seamless-local-pairing.md) | 原因、loopback/Origin/CSRF 边界、全量验证、备份重启与现场空 Cookie 结果 |
| [最终一致性检查](final-audit.json) | 全量后两份源码的修复范围与对应回归、依赖锁一致、wheel 指纹、OpenAPI 与本地文档链接 |
| [用户数据备份](model-settings-backup.json) / [重启检查](model-settings-restart.json) | 维护排空、备份、API/Worker/数据库健康；不包含凭据 |
| [MCP 兼容记录](../compatibility/mcp.md) / [Runtime 记录](../../harness/runtime/COMPATIBILITY.md) | 实测依赖组合、传输和持久化边界 |

可复现命令位于[项目 README](../../README.md)。`scripts/verify_all.py` 归档完整后端检查；`scripts/verify_wheel.py` 在临时目录创建独立虚拟环境，验证当时生成的 wheel；两者均将结果持久写入本目录。安装包位于 `dist/`，启动入口为 `harness serve`。

尚未验收：真实云端/本地模型、第三方 MCP/OAuth 账号、36 项真实模型效果基准、建议的 8 小时稳定性和 20 并发性能目标、其他操作系统与 Python 版本。没有将 fixture、短时冒烟或确定性 Demo 结果登记成这些项目通过；P2 扩展仍按原设计范围保留。
