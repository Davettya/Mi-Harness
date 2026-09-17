# Harness Web 工作台

React + TypeScript 工作台，依据 [11-web](../../docs/implementation/11-web.md) 和 [10-api-events](../../docs/implementation/10-api-events.md) 实现。浏览器只呈现服务端事实；任务、授权、操作账本和配置版本由后端持有。

## 构建与开发

在项目根目录安装 Python 锁定依赖后，导出真实 FastAPI 契约：

```powershell
.venv/Scripts/python.exe apps/web/scripts/export_openapi.py
```

在 `apps/web` 中运行：

```powershell
npm ci --no-audit --no-fund
npm run generate:api
npm test
npm run build
```

生产 API 自动提供 `dist/`，wheel 构建将其放入 `harness/resources/web`。`harness serve --open` 自动打开工作台，前端消费 URL fragment 中的短期一次性启动凭证并立即清除，换取登录 Cookie。普通使用无需手填口令；`harness pair` 保留为备用入口。本地开发可运行 `npm run dev`，在 loopback 5173 提供页面并代理到 8767，开发入口可用备用配对。

当前界面采用项目分组侧栏：所有项目同时显示，每个项目支持多个源文件夹，组内列出独立会话。新建会话即时持久化，项目设置支持原生目录选择及手填路径。细节见 [项目实现说明](../../docs/implementation/16-projects-and-local-launch.md)。

## 契约与边界

- `src/api/generated/schema.ts` 从 `docs/generated/openapi.json` 生成，勿手工维护。`api/client.ts` 是具备 CSRF、幂等重试和错误处理的 facade。
- SSE 按耐久 seq 去重和检测缺口；临时文本独立缓存，重连不补发旧临时片段，完整消息替换临时内容。连接断开不会取消任务。
- 草稿与待重试命令仅保留在当前页面内存。配置、交互、核对、steering 与上下文固定内容使用相应 revision，不根据点击结果推断成功。
- 模型回复（含流式输出和历史消息）使用 `react-markdown` + `remark-gfm` 渲染，支持标题、强调、列表、引用、表格、任务清单、删除线及代码块。用户输入、工具输出和原始产物文本继续按纯文本显示。
- Markdown 不解析原始 HTML；链接仅允许绝对 HTTP(S) 地址，新标签页使用 `noopener noreferrer`；图片语法显示为链接，避免自动请求不可信远程图片。未闭合的流式代码围栏可直接显示，完整消息到达后使用同一组件。
- HTML/SVG 产物使用无权限 iframe 与 restrictive CSP；PDF 与超限文件提供下载。页面不执行产物脚本。
- 模型和 MCP 配置只保存凭据引用。演示模型明确标注为确定性本地演示；其验收不能代替真实供应商兼容性验证。
- 插件管理只导入宿主支持的静态声明，不安装任意代码；清单来源不会直接授予权限。

浏览器验收与自动化覆盖见 [VERIFICATION.md](VERIFICATION.md)。
