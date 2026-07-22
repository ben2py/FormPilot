from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from urllib.parse import urlparse


ApiMode = Literal["auto", "responses", "chat"]


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


def resolve_api_mode(
    model: str,
    *,
    api_mode: ApiMode | str = "auto",
    base_url: str | None = None,
) -> Literal["responses", "chat"]:
    """Pick Responses vs Chat Completions based on config and provider hints."""
    mode = (api_mode or "auto").strip().lower()
    if mode in {"responses", "chat"}:
        return mode  # type: ignore[return-value]
    if mode != "auto":
        raise ValueError(f"Unsupported FORMPILOT_API_MODE: {api_mode!r}")

    model_l = (model or "").lower()
    resolved_base = (base_url if base_url is not None else os.getenv("OPENAI_BASE_URL") or "").lower()
    host = urlparse(resolved_base).hostname or ""
    if "deepseek" in model_l or "deepseek" in host or "deepseek" in resolved_base:
        return "chat"
    return "responses"


def create_model(
    model: str,
    reasoning_effort: str = "medium",
    *,
    api_mode: ApiMode | str = "auto",
    client: Any | None = None,
) -> AgentModel:
    mode = resolve_api_mode(model, api_mode=api_mode)
    if mode == "chat":
        return OpenAIChatCompletionsModel(model, reasoning_effort, client=client)
    return OpenAIResponsesModel(model, reasoning_effort, client=client)


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


def _responses_tools_to_chat(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        if "function" in tool and isinstance(tool["function"], dict):
            converted.append(tool)
            continue
        function: dict[str, Any] = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        if "strict" in tool:
            function["strict"] = tool["strict"]
        converted.append({"type": "function", "function": function})
    return converted


def _map_deepseek_reasoning_effort(effort: str) -> str | None:
    """Map FormPilot effort levels onto DeepSeek's supported values."""
    normalized = (effort or "medium").strip().lower()
    if normalized in {"", "none", "off", "disabled"}:
        return None
    if normalized in {"xhigh", "max"}:
        return "max"
    # DeepSeek maps low/medium to high for compatibility.
    return "high"


def _serialize_chat_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for call in tool_calls or []:
        function = getattr(call, "function", None)
        serialized.append(
            {
                "id": getattr(call, "id", ""),
                "type": getattr(call, "type", None) or "function",
                "function": {
                    "name": getattr(function, "name", ""),
                    "arguments": getattr(function, "arguments", None) or "{}",
                },
            }
        )
    return serialized


def _chat_history_to_messages(instructions: str, input_items: list[Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
    for item in input_items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item.get("role") == "user" and item_type is None:
            messages.append({"role": "user", "content": item.get("content") or ""})
            continue
        if item_type == "chat_message":
            message: dict[str, Any] = {
                "role": "assistant",
                "content": item.get("content"),
            }
            if item.get("reasoning_content") is not None:
                # DeepSeek thinking + tools requires reasoning_content to be echoed back.
                message["reasoning_content"] = item["reasoning_content"]
            if item.get("tool_calls"):
                message["tool_calls"] = item["tool_calls"]
            messages.append(message)
            continue
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": item.get("output") or "",
                }
            )
    return messages


class OpenAIChatCompletionsModel:
    """Chat Completions adapter for DeepSeek V4 and other OpenAI-compatible providers."""

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
        messages = _chat_history_to_messages(instructions, input_items)
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": _responses_tools_to_chat(tools),
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        mapped_effort = _map_deepseek_reasoning_effort(self.reasoning_effort)
        if mapped_effort is None:
            create_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            create_kwargs["reasoning_effort"] = mapped_effort
            create_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        response = await self.client.chat.completions.create(**create_kwargs)
        message = response.choices[0].message
        serialized_tool_calls = _serialize_chat_tool_calls(getattr(message, "tool_calls", None))
        output_item = {
            "type": "chat_message",
            "role": "assistant",
            "content": message.content,
            "reasoning_content": getattr(message, "reasoning_content", None),
            "tool_calls": serialized_tool_calls or None,
        }

        calls: list[ToolCall] = []
        for call in serialized_tool_calls:
            raw_arguments = call["function"].get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"_invalid_json": raw_arguments}
            if not isinstance(arguments, dict):
                arguments = {"_invalid_json": raw_arguments}
            calls.append(
                ToolCall(
                    call_id=call["id"],
                    name=call["function"].get("name") or "",
                    arguments=arguments,
                )
            )

        return ModelTurn(
            output_items=[output_item],
            tool_calls=calls,
            text=message.content or "",
        )
