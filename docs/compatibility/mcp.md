# MCP、Skills、插件与本地服务兼容记录

记录日期：2026-09-17。执行环境：Windows、CPython 3.11.9，项目 `.venv`。
本文件记录实际源码检查与本地 fixture 结果，不把本地兼容外推为第三方生产服务已验收。

## 锁版本决策

使用官方 `mcp==2.2.0`（其 HTTP 依赖为 `httpx2==2.13.0`），直接包装官方 `mcp.Client`。
服务 fixture 使用 `mcp.server.mcpserver.MCPServer`；该类型不能从 `mcp` 包根导入。
不依赖旧 `FastMCP` 包或 beta `langchain.mcp.MCPAdapter`。LangChain 仍通过现有 Tool Gateway
调用 MCP 工具，SDK 包装没有引入第二个模型工具循环或第二本副作用账本。

选择依据是官方 [v2.2.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.2.0)、
[锁版本 Client 源码](https://github.com/modelcontextprotocol/python-sdk/blob/v2.2.0/src/mcp/client/client.py)、
[协议版本说明](https://github.com/modelcontextprotocol/python-sdk/blob/v2.2.0/docs/protocol-versions.md)；
随后用实际安装源码和 `inspect.signature` 检查 API，再执行本地网络/进程互操作。

## 已实测的 API 形状

| API | 本项目实际使用的形状 |
|---|---|
| Client | `Client(target, mode='auto'/'legacy', elicitation_callback=..., input_required_max_rounds=8, cache=None)` |
| 协商 | `client.protocol_version`，modern-only 在协商后拒绝旧版本，不手工实现 wire 初始化 |
| tools/list | `await client.list_tools(cursor=opaque_cursor)`；空串继续，只有 null/缺失结束 |
| tools/call | `await client.call_tool(name, arguments)`；结果字段 `structured_content`、`is_error` |
| MRTR 单步 | `await client.session.call_tool(name, arguments, input_responses=..., request_state=..., allow_input_required=True)` |
| HTTP | `streamable_http_client(url, http_client=httpx2.AsyncClient(...))` |
| stdio | `stdio_client(StdioServerParameters(...), errlog=bounded_pipe)` |
| OAuth | `OAuthClientProvider(server_url, client_metadata, storage, redirect_handler, callback_handler, validate_resource_url)` |
| Resources/Prompts | `read_resource(uri)`、`get_prompt(name, arguments)`，保留 server 与内容来源 |

SDK 2.2.0 的 legacy 版本为 `2025-11-25`，modern 为 `2026-07-28`。
HTTP modern 实测请求级 SSE 与 JSON；stdio 两代均使用真实子进程。

## 验证结果与实际边界

| 场景 | 结果与证据 |
|---|---|
| S01 | 安全 YAML、错误条目保留、同名来源、固定 hash、资源逃逸、完整性篡改、权限撤销；`tests/test_skills.py` |
| S02 | factory 白名单、注册回滚、依赖环、版本 drain、等待 run 后续调用、Registry 重新构造恢复；`tests/test_plugins.py` |
| P01 | 现代/legacy × stdio/Streamable HTTP，HTTP JSON/SSE，退出 context 后重入调用；`tests/test_mcp.py` |
| P02 | MCP 业务错误、协议异常分类、请求取消、本地取消不冒充远端撤销；同上 |
| P03 | 空 cursor、重复 cursor、schema 错误、身份缓存分区、安全别名；同上 |
| P04 | 显式 P0 elicitation cancel 与 `unsupported_elicitation`，诊断不执行工具；同上 |
| MP05 OAuth | 官方 SDK 的发现、动态注册、PKCE、callback state/issuer、vault 分区；`tests/test_mcp_oauth.py` |
| MP05 耐久 OAuth | 新建服务对象后 callback 继续、PKCE 校验、callback 防重放、业务库无 state/verifier/token 明文；`tests/test_mcp_authorization.py` |
| MP05 生产 MRTR | 真本地 MCP HTTP JSON/SSE → Tool Gateway 审批 → LangGraph checkpoint/表单 → 服务对象与 SQLite saver 重建 → 固定原配置续接。计数证明初始调用一次、续接一次、operation 与预算只结算一次；`tests/test_mcp_continuation.py` |
| S02 生产生命周期 | 声明安装/撤销实际写入宿主配置、旧run等待期间drain、更新默认、重启保留旧锁、终态释放与旧版本dispose；`tests/test_plugins_integration.py` |
| PF01 | 真临时 API/worker/supervisor 子进程、重复启动、实例/创建时间检查、安全 stop、不终止其他进程；`tests/test_platform.py` |
| PF01 配置实际生效 | 真生产supervisor/API/worker，HTTP配对/提交，指定模型进入快照，Worker受workspace_write上界拒绝；network/process诊断拒绝及主机列表收窄；`tests/test_platform_config_integration.py` |
| S01/SEC 生产补充 | 命名策略固定且当前撤销继续生效；来源信任/启停无需扫描即可撤销；非UTF8资源经Gateway转Artifact引用；`tests/test_config_policy_skills_integration.py` |
| D01 | 停写门禁、一致性备份、新目录恢复、保留原数据、skill snapshot 内容回读；`tests/test_platform.py` |

以上首轮组合命令共 **32 项通过**（2026-09-17，10.41 秒）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_skills.py tests/test_plugins.py tests/test_mcp.py tests/test_mcp_oauth.py tests/test_mcp_authorization.py tests/test_platform.py -q --disable-warnings
```

同日第二轮上述范围扩展为 **41 项通过 / 85.68 秒**，加入生产插件、配置与MRTR恢复测试。
随后新增 Windows 状态原子替换重试测试 **1 项通过 / 0.27 秒**，命名策略/Skill资源生产测试 **2 项通过 / 2.19 秒**，legacy profile启用elicitation标志时普通工具仍可运行的生产网关测试 **2 项通过 / 3.70 秒**。这是46项不同测试的分批记录；最终全量结果以实施计划核对清单中的统一命令为准。

最后加入 stdio 凭据引用/服务target绑定/最小环境测试：MCP、平台、OAuth相关 **28 项通过 / 13.56 秒**。该用例使用 CredentialVault 的实际引用生成与target校验、可控keyring后端替身，以及真正SDK stdio子进程；它证明API_KEY等环境变量名可引用凭据、不会复制宿主canary，并且跨服务target被拒绝。累计本模块范围47个不同用例；不将后端替身结果表述为真实用户凭据验收。

现代 `InputRequiredResult` 与 legacy `ctx.elicit` 的服务端行为不同。legacy HTTP JSON-only fixture
没有反向请求通道：其 elicitation 明确返回 protocol_error，不能报告 callback 成功；legacy SSE
与现代 JSON/SSE 的 callback 已实测通过。耐久人工等待只在现代 MRTR 路径启用。
生产 legacy 客户端可以执行普通工具；收到人工输入请求时明确cancel，不把内存callback测试宣称为跨重启的人机交互支持。
OAuth 的网络 fixture 使用受控 `httpx2.MockTransport`，没有使用真实账户或对公网账户授权。

第三方生产服务兼容、真实授权服务器与实际用户账号、远端 Skills 的特定扩展 revision、8 小时稳定性
仍需相应环境的专项验收。远端 Skills 缺少明确能力/revision 时拒绝启用，不把本地 Skill 结果替代远端验收。

## 宿主集成边界

`register_catalog` 的服务器 annotations 只保留来源提示，默认将 MCP 工具归为外部写操作并要求
普通 Tool Gateway 授权；只有宿主显式配置才可声明只读。执行器校验 ledger、grant、fence、
参数与 OperationKey，禁止诊断身份执行工具。SDK 出站请求与 OAuth 发现/刷新分别注入统一策略。

OAuth 浏览器 state、PKCE verifier、authorization code 与 token 不写业务库或常规日志。
现代 continuation requestState 只保存到平台 vault；数据库引用与 interaction 原子关联。
发送后失联保留未知结果，没有按 429/503 或网络异常自动重复写操作。
