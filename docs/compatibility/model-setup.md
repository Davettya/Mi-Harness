# 简化模型配置：预设来源与验收边界

## 本机 TUN / Fake-IP 兼容修订

2026-09-17 在用户本机确认 `api.deepseek.com` 解析为 `198.18.0.88`，原公网地址检查在请求发送前返回 `SSRF_DENIED`。修订只对已授权且完整 URL 命中官方目录的 HTTPS 模型端点接受 `198.18.0.0/15` Fake-IP；保留原始主机名的 TLS 证书校验、禁止重定向、local_only 与主机限制。任意自定义地址、HTTP、MCP、通用网络工具、其他非公网地址仍不适用。Clash 配置未被修改。

新增回归见 `tests/test_model_fake_ip.py`。真实网络验收仅发送不含 API Key 的 HTTPS 请求，确认到达供应商认证层；不据此声称用户密钥、模型名称、账户额度或完整模型协议已通过。

2026-09-17 修订。交互按用户提供的 CodeBuddy/WorkBuddy 截图设计：提供方、API Key、模型名称，测试连接与保存并使用。HTTP 契约归 [10](../implementation/10-api-events.md)，页面归 [11](../implementation/11-web.md)，模型/端点权限归 [03](../implementation/03-model-gateway.md) 与 [12](../implementation/12-platform.md)。

提供方目录由 `harness/model_gateway/presets.py` 唯一维护，页面读取该目录，不重复硬编码地址。以下为本次核验的官方来源；预设名称仅为可选项，不能证明当前账号可用或模型能力已验证。

| 提供方 | 默认地址 | 模型目录与来源 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | [模型文档](https://developers.openai.com/api/docs/models)、[模型目录 API](https://developers.openai.com/api/reference/resources/models/methods/list)；使用 Responses 协议 |
| Anthropic | `https://api.anthropic.com` | [模型目录 API](https://platform.claude.com/docs/en/api/models/list)；使用 Messages 协议 |
| DeepSeek | `https://api.deepseek.com` | [官方 API 文档](https://api-docs.deepseek.com/)；使用 OpenAI 兼容协议 |
| 通义千问（百炼·北京） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | [兼容接口与地区说明](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)；页面标明北京，其他地区使用自定义服务 |
| 腾讯云 Token Plan | `https://api.lkeap.cloud.tencent.com/plan/v3` | [接入说明](https://cloud.tencent.com/document/product/1823/130066)、[模型目录说明](https://cloud.tencent.com/document/product/1823/136601)；当前使用官方预设，不宣称已从账号发现模型 |
| 硅基流动 | `https://api.siliconflow.cn/v1` | [模型目录 API](https://docs.siliconflow.cn/docs/api/models-get)；查询 chat 类型 |
| Ollama（本机） | `http://127.0.0.1:11434` | [本机模型目录](https://docs.ollama.com/api/tags)；无需 API Key |
| 自定义 OpenAI 兼容 | 用户填写 | HTTPS 或本机 HTTP；固定地址授权、拒绝携带凭据的 URL 与跳转，不自动继承其他提供方的 Key |

发现服务的目录与实际协议检查分开：目录无法枚举全部条目时允许输入完整模型名称；固定协议检查不执行工作区工具。当前新建模型采用有来源的应用输入/输出上限，不将它伪装成供应商标称上下文或费用。已保存能力必须有本次检查证据，不继承另一个 Key、模型或地址的已验证状态。

验收使用本机可控 HTTP 服务、独立临时数据目录和测试凭据，未连接真实供应商账户。本次结果在[实施计划追加清单](../implementation/14-delivery-plan.md)与[Web 验收记录](../../apps/web/VERIFICATION.md)记录；不以界面截图或目录返回替代真实云端效果验收。
