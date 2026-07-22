from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ModelTurn:
    output_items: list[Any] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = ""


class AgentModel(Protocol):
    async def complete(
        self,
        *,
        input_items: list[Any],
        tools: list[dict[str, Any]],
        instructions: str,
    ) -> ModelTurn: ...


class OpenAIResponsesModel:
    """Thin adapter around the Responses API tool-calling protocol."""

    def __init__(self, model: str, reasoning_effort: str = "medium", client: Any | None = None) -> None:
        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI()
        self.client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def complete(
        self,
        *,
        input_items: list[Any],
        tools: list[dict[str, Any]],
        instructions: str,
    ) -> ModelTurn:
        response = await self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_items,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            reasoning={"effort": self.reasoning_effort},
            store=False,
        )
        calls: list[ToolCall] = []
        for item in response.output:
            if getattr(item, "type", None) != "function_call":
                continue
            raw_arguments = getattr(item, "arguments", "{}") or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw_arguments}
            calls.append(
                ToolCall(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=arguments,
                )
            )
        return ModelTurn(
            output_items=list(response.output),
            tool_calls=calls,
            text=response.output_text or "",
        )
