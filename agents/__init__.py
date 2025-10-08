from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel


TModel = TypeVar("TModel", bound=BaseModel)


@dataclass
class ModelSettings:
    """Light-weight container mirroring the real agent settings structure."""

    store: bool = False
    reasoning: Optional[Any] = None


class WebSearchTool:
    """Stubbed web search tool placeholder.

    The production system uses a richer tool implementation. For backend
    integration tests we expose a minimal async-compatible interface that keeps
    track of the configuration but delegates execution to the calling domain.
    """

    def __init__(
        self,
        filters: Optional[Dict[str, Any]] = None,
        search_context_size: str = "medium",
        user_location: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.filters = filters or {}
        self.search_context_size = search_context_size
        self.user_location = user_location or {}

    async def arun(self, query: str, *, max_results: int = 8) -> Dict[str, Any]:
        """Placeholder async search call.

        Returns an empty payload so the agent can still reason deterministically
        when the hosting environment does not provide a real web-search backend.
        """

        return {
            "query": query,
            "results": [],
            "meta": {
                "max_results": max_results,
                "filters": self.filters,
                "search_context_size": self.search_context_size,
                "user_location": self.user_location,
            },
        }


TResponseInputItem = Dict[str, Any]


class Agent:
    """Very small adapter that mimics the OpenAI Agents SDK surface area.

    The objective is to let the application reuse a declarative agent interface
    while routing execution through the Chat Completions API that is already
    configured in the project. The adapter converts an agent conversation into a
    prompt, invokes the model, and validates the structured response using a
    Pydantic schema supplied by the caller.
    """

    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        model: str,
        tools: Optional[Sequence[WebSearchTool]] = None,
        output_type: Type[TModel],
    model_settings: Optional[ModelSettings] = None,
    client: Optional[AsyncOpenAI] = None,
    temperature: Optional[float] = None,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.model = model
        self.tools = list(tools or [])
        self.output_type = output_type
        self.model_settings = model_settings or ModelSettings()
        self.temperature = temperature
        self._client = client or AsyncOpenAI()

    async def arun(
        self,
        *,
        conversation_history: Optional[Sequence[TResponseInputItem]] = None,
        input_text: Optional[str] = None,
        extra_system_messages: Optional[Iterable[str]] = None,
    ) -> Tuple[TModel, str]:
        """Execute the agent plan and return the validated response.

        Parameters
        ----------
        conversation_history:
            Prior turns encoded as dictionaries containing role/content pairs.
        input_text:
            User text for the current turn. When omitted only prior conversation
            is used.
        extra_system_messages:
            Optional iterable of additional system instructions appended after
            the default agent instructions.
        """

        messages: List[ChatCompletionMessageParam] = [
            {"role": "system", "content": self.instructions},
        ]

        if extra_system_messages:
            for msg in extra_system_messages:
                if msg:
                    messages.append({"role": "system", "content": msg})

        if conversation_history:
            for entry in conversation_history:
                role = entry.get("role")
                content = entry.get("content")
                if role and content:
                    if isinstance(content, list):
                        # Guard for multi-content payloads (text + attachments).
                        text_parts = []
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "input_text":
                                text_parts.append(part.get("text", ""))
                            elif isinstance(part, str):
                                text_parts.append(part)
                        content = "\n".join(part for part in text_parts if part)
                    messages.append({"role": role, "content": str(content)})

        if input_text:
            messages.append({"role": "user", "content": input_text})

        request_payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if self.temperature is not None:
            request_payload["temperature"] = self.temperature

        response = await self._client.chat.completions.create(**request_payload)

        content = response.choices[0].message.content or ""
        parsed_json = self._extract_first_json_block(content)
        validated = self.output_type.model_validate(parsed_json)
        return validated, content

    @staticmethod
    def _extract_first_json_block(value: str) -> Dict[str, Any]:
        first = value.find("{")
        last = value.rfind("}")
        if first == -1 or last == -1 or last <= first:
            raise ValueError("No JSON object detected in agent response")
        try:
            return json.loads(value[first : last + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to decode agent JSON: {exc}")


__all__ = [
    "Agent",
    "ModelSettings",
    "TResponseInputItem",
    "WebSearchTool",
]
