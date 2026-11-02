"""DeepSeek Chat Completions with reasoning preserved across tool calls.

ChatOpenAI deliberately omits provider-specific reasoning fields. Keep DeepSeek's field in
additional_kwargs so checkpoints and helper transcripts retain it without displaying it as text.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


class DeepSeekChatOpenAI(ChatOpenAI):
    """OpenAI-compatible client that receives and replays DeepSeek reasoning_content."""

    def _create_chat_result(
        self, response: Any, generation_info: dict[str, Any] | None = None
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        raw = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, raw["choices"], strict=True):
            reasoning = choice["message"].get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict[str, Any],
        default_chunk_class: type,
        base_generation_info: dict[str, Any] | None,
    ) -> ChatGenerationChunk | None:
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        choices = chunk.get("choices") or []
        if result and isinstance(result.message, AIMessageChunk) and choices:
            reasoning = (choices[0].get("delta") or {}).get("reasoning_content")
            if isinstance(reasoning, str):
                result.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _get_request_payload(
        self, input_: Any, *, stop: list[str] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)
        # DeepSeek requires the field on historical assistant messages when tools are bound,
        # including final answers. Old non-thinking turns and synthetic summaries have none;
        # an empty field preserves those messages without inventing a reasoning trace.
        for message, encoded in zip(messages, payload["messages"], strict=True):
            if isinstance(message, AIMessage):
                reasoning = message.additional_kwargs.get("reasoning_content")
                encoded["reasoning_content"] = reasoning if isinstance(reasoning, str) else ""
        return payload
