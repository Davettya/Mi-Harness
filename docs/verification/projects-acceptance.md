# 项目与本机连接升级验收

日期：2026-09-18，Windows，本次检出的源码和最终前端构建。

## 自动检查

- 后端全量：**138 passed**，耗时 625.42 秒。记录：`projects-pytest.xml` / `projects-pytest.log`。
- 项目定向回归：**4 passed**。在全量之后补充第二源文件夹经批准写入的检查，记录：`projects-focused.xml`。
- 前端：**22 passed / 5 files**，包含自动连接单次交换及项目分页。
- Python 编译、Ruff 致命错误检查、TypeScript 与 Vite 生产构建通过。
- wheel 独立安装、包内 Web/Skill、监督进程/API/worker 启动、Demo 图执行完成及停止均通过；最终包 hash 与结果见 `wheel-verification.json`，构建产物清单见 `projects-build.json`。
- pytest 存在一条 Starlette/AnyIO 的废弃 API 警告，无测试失败。

## 浏览器验收（独立临时数据，8878 端口）

1. 启动 fragment 换取登录成功，地址栏恢复 `/`；刷新无需配对。
2. 两个项目同时显示，分别展示所属会话及源文件夹数量。
3. 项目 `＋` 按钮立即持久化并显示新会话。
4. 发送首条消息后自动命名，Demo 运行达到 completed；此项验证流程，不代表真实供应商质量。
5. 切换到另一项目中的会话，再刷新，项目/会话选择保持一致。
6. 从项目设置添加第二个源文件夹并保存，栏目数量更新，已有会话保留。
7. 原生目录选择实际返回本机路径，页面自动填入目录和默认项目名；关闭该测试表单，没有把测试选择的桌面目录保存为项目。
8. 最终侧栏长标题截断、目录数量单行展示；可折叠各项目，项目列表独立滚动。

原生选择的成功路径在真实 Windows 上观察，取消分支通过 Mock 接口测试验证；不宣称逐个完成所有权限失败、超时及 Windows 桌面环境的实机验收。

## 原有数据与服务

升级前未完成运行数为 0。执行协调备份并停止原服务，使用原 `effective_config.settings` 重启；启动入口实际打开浏览器成功。逐项核对原项目 ID、名称、主目录和会话 ID 列表保持一致，运行配置除新实例 ID 外一致，数据库 integrity/foreign key 检查通过。

备份数据由 CLI 返回到 `%LOCALAPPDATA%\LocalAgentHarness-backups\projects-20260918\data`；本机 Store Python 的文件系统重定向可能使物理目录位于 Python 应用包的 LocalCache 下。运行配置与升级前项目清单单独保存于 `C:\Users\Dave Tuo\AppData\Local\LocalAgentHarness-backups\projects-20260918`。

## 边界

- 同项目会话共享真实文件，不是独立 worktree。
- 非 Windows / 无交互桌面使用手填目录；原生窗口提供超时与父服务退出清理。
- 项目存在非终态运行时不能变更源文件夹，防止审批期间改变文件访问范围。
- 原内部 `workspace_id` 和旧单目录接口为兼容保留；用户界面已使用“项目”。
