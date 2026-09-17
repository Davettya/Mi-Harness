"""Provider-specific adapters. Imports and credential resolution happen at use time."""

from __future__ import annotations

import json
import re
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from .profiles import GatewayError, ModelProfile
from .transports import bounded_transport


class DemoChatModel(BaseChatModel):
    """Explicitly deterministic local test/demo model; no intelligence claim.

    /tool NAME {JSON} requests one tool; ordinary text returns a labelled echo.
    A fresh instance can resume entirely from messages, with no hidden cursor.
    """

    model_name: str = "deterministic-demo-v1"
    bound_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "harness-deterministic-demo"

    def bind_tools(self, tools, **kwargs):
        names = [t.get("function", t).get("name") if isinstance(t, dict) else t.name for t in tools]
        return self.model_copy(update={"bound_names": names})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        last = messages[-1]
        if isinstance(last, ToolMessage):
            try:
                data = json.loads(last.content)
            except (TypeError, ValueError):
                data = {}
            child_id = data.get("structured_data", {}).get("child_run_id")
            if child_id and "wait_children" in self.bound_names:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "wait_children",
                            "args": {"child_run_ids": [child_id], "mode": "all"},
                            "id": str(uuid4()),
                            "type": "tool_call",
                        }
                    ],
                )
            else:
                summary = data.get("summary", last.content)
                response_text = data.get("structured_data", {}).get("response", {}).get("text")
                if response_text:
                    summary += "：" + response_text
                answer = AIMessage(content=f"[本地确定性演示] {summary}", id=str(uuid4()))
        else:
            user = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), last)
            text = (
                user.content
                if isinstance(user.content, str)
                else json.dumps(user.content, ensure_ascii=False)
            )
            if text.startswith("HARNESS_COMPACTION_FIXTURE_V1\n"):
                fixture = json.loads(text.split("\n", 1)[1])
                user_texts = [
                    str(item["data"].get("content", ""))
                    for item in fixture["messages"]
                    if item["type"] == "human"
                ]
                compacted = {
                    "goal": "[确定性摘要fixture] " + (user_texts[-1][:120] if user_texts else "continue"),
                    "constraints": fixture["hard_constraints"],
                    "verified_facts": [],
                    "changes": [],
                    "evidence_refs": [],
                    "failed_approaches": [],
                    "open_items": ["演示摘要不评估任务语义；原始消息可按来源查看"],
                    "next_steps": [],
                }
                answer = AIMessage(
                    content=json.dumps(compacted, ensure_ascii=False),
                    response_metadata={"finish_reason": "stop", "demo": True},
                )
                return ChatResult(generations=[ChatGeneration(message=answer)])
            match = re.fullmatch(r"\s*/tool\s+([\w.-]+)\s+(\{.*\})\s*", text, re.DOTALL)
            if match:
                name, raw = match.groups()
                if name not in self.bound_names:
                    answer = AIMessage(content=f"[本地确定性演示] 未开放工具：{name}")
                else:
                    answer = AIMessage(
                        content="",
                        tool_calls=[
                            {"name": name, "args": json.loads(raw), "id": str(uuid4()), "type": "tool_call"}
                        ],
                    )
            else:
                answer = AIMessage(content=f"[本地确定性演示，非真实模型] {text}")
        answer.usage_metadata = {
            "input_tokens": max(1, sum(len(str(m.content)) for m in messages) // 2),
            "output_tokens": max(1, len(str(answer.content)) // 2),
            "total_tokens": 1,
        }
        answer.usage_metadata["total_tokens"] = (
            answer.usage_metadata["input_tokens"] + answer.usage_metadata["output_tokens"]
        )
        answer.response_metadata = {
            "finish_reason": "tool_calls" if answer.tool_calls else "stop",
            "demo": True,
        }
        return ChatResult(generations=[ChatGeneration(message=answer)])


def build_provider_model(
    profile: ModelProfile, endpoint: str, credential: str | None = None
) -> BaseChatModel:
    import httpx

    modes = {
        "openai": {"chat_completions", "responses"},
        "anthropic": {"messages"},
        "ollama": {"chat"},
        "demo": {"fixture"},
    }
    if profile.api_mode not in modes[profile.adapter_id]:
        raise GatewayError("invalid_api_mode", "API mode does not match the selected adapter")
    if not profile.model_id:
        raise GatewayError("invalid_model", "Model ID is required")
    if profile.adapter_id == "demo":
        return DemoChatModel()
    if profile.adapter_id == "openai":
        from langchain_openai import ChatOpenAI

        if not credential:
            if profile.provider_id != "custom":
                raise GatewayError("missing_credential", "OpenAI profile has no configured credential")
            credential = "not-required"
        return ChatOpenAI(
            model=profile.model_id,
            base_url=endpoint,
            api_key=credential,
            max_retries=0,
            timeout=60,
            max_tokens=profile.limits.output_limit,
            use_responses_api=profile.api_mode == "responses",
            http_client=httpx.Client(
                trust_env=False, follow_redirects=False, timeout=60, transport=bounded_transport(httpx)
            ),
            http_async_client=httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=60,
                transport=bounded_transport(httpx, asynchronous=True),
            ),
        )
    if profile.adapter_id == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not credential:
            raise GatewayError("missing_credential", "Anthropic profile has no configured credential")
        import anthropic
        import httpx2 as anthropic_httpx

        model = ChatAnthropic(
            model=profile.model_id,
            base_url=endpoint,
            api_key=credential,
            max_retries=0,
            timeout=60,
            max_tokens=profile.limits.output_limit or 1024,
        )
        # These are cached properties in locked langchain-anthropic 1.7.2. Seed them
        # with SDK clients using explicit transports; ambient proxy vars are not policy.
        model.__dict__["_client"] = anthropic.Anthropic(
            api_key=credential,
            base_url=endpoint,
            max_retries=0,
            http_client=anthropic_httpx.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=60,
                transport=bounded_transport(anthropic_httpx),
            ),
        )
        model.__dict__["_async_client"] = anthropic.AsyncAnthropic(
            api_key=credential,
            base_url=endpoint,
            max_retries=0,
            http_client=anthropic_httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=60,
                transport=bounded_transport(anthropic_httpx, asynchronous=True),
            ),
        )
        return model
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=profile.model_id,
        base_url=endpoint,
        num_predict=profile.limits.output_limit,
        client_kwargs={"trust_env": False, "follow_redirects": False, "timeout": 60},
        sync_client_kwargs={"transport": bounded_transport(httpx)},
        async_client_kwargs={"transport": bounded_transport(httpx, asynchronous=True)},
    )
