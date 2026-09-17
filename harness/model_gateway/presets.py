"""Official provider connection presets. Names are suggestions, never verified capabilities."""

from copy import deepcopy


def _preset(
    identity,
    name,
    endpoint,
    models,
    docs,
    *,
    key=True,
    custom=False,
    adapter="openai",
    mode="chat_completions",
    discovery="/models",
):
    return {
        "id": identity,
        "name": name,
        "requires_api_key": key,
        "default_base_url": endpoint,
        "allow_custom_base_url": custom,
        "models": [{"id": m, "name": m} for m in models],
        "documentation_url": docs,
        "adapter": adapter,
        "api_mode": mode,
        "discovery_path": discovery,
    }


PRESETS = {
    p["id"]: p
    for p in [
        _preset(
            "openai",
            "OpenAI",
            "https://api.openai.com/v1",
            ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"],
            "https://developers.openai.com/api/docs/models",
            mode="responses",
        ),
        _preset(
            "anthropic",
            "Anthropic",
            "https://api.anthropic.com",
            ["claude-sonnet-4-6", "claude-opus-5"],
            "https://platform.claude.com/docs/en/api/models/list",
            adapter="anthropic",
            mode="messages",
            discovery="/v1/models",
        ),
        _preset(
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com",
            ["deepseek-flash", "deepseek-v4-pro"],
            "https://api-docs.deepseek.com/",
        ),
        _preset(
            "qwen",
            "通义千问（百炼·北京）",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ["qwen-plus", "qwen3.8-max"],
            "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
            discovery=None,
        ),
        _preset(
            "tencent",
            "腾讯云 Token Plan",
            "https://api.lkeap.cloud.tencent.com/plan/v3",
            ["tc-code-latest", "glm-5.1", "minimax-m2.7", "kimi-k2.5"],
            "https://cloud.tencent.com/document/product/1823/130066",
            discovery=None,
        ),
        _preset(
            "siliconflow",
            "硅基流动",
            "https://api.siliconflow.cn/v1",
            ["deepseek-ai/DeepSeek-V4-Flash", "Pro/zai-org/GLM-5.1"],
            "https://docs.siliconflow.cn/docs/api/models-get",
            discovery="/models?sub_type=chat",
        ),
        _preset(
            "ollama",
            "Ollama（本机）",
            "http://127.0.0.1:11434",
            [],
            "https://docs.ollama.com/api/tags",
            key=False,
            adapter="ollama",
            mode="chat",
            discovery="/api/tags",
        ),
        _preset(
            "custom",
            "自定义 OpenAI 兼容",
            "",
            [],
            "https://developers.openai.com/api/reference/resources/models/methods/list",
            key=False,
            custom=True,
        ),
    ]
}


def provider_catalog():
    return {
        "items": [
            {k: deepcopy(v) for k, v in p.items() if k not in {"adapter", "api_mode", "discovery_path"}}
            for p in PRESETS.values()
        ]
    }
