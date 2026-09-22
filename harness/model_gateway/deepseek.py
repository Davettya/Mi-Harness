"""Preserve DeepSeek's mandatory thinking continuation across tool turns."""
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI


class DeepSeekChatModel(ChatOpenAI):
    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        data = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, data.get("choices", [])):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(self, chunk, default_chunk_class, base_generation_info):
        result = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices", [])
        if result is not None and choices:
            reasoning = choices[0].get("delta", {}).get("reasoning_content")
            if reasoning is not None:
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        # Recent OpenAI SDK adapters rename max_tokens; DeepSeek documents only
        # max_tokens, so leaving the alias can silently lose the output bound.
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        for message, wire in zip(messages, payload.get("messages", [])):
            if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
                wire["reasoning_content"] = message.additional_kwargs["reasoning_content"]
        return payload
