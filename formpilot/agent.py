from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import AgentModel
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


TraceHandler = Callable[[str], None]


@dataclass(slots=True)
class AgentResult:
    text: str
    steps: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class FormPilotAgent:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolRegistry,
        *,
        max_steps: int = 30,
        trace: TraceHandler | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.trace = trace

    def _log(self, message: str) -> None:
        if self.trace:
            self.trace(message)

    async def run(self, goal: str) -> AgentResult:
        input_items: list[Any] = [{"role": "user", "content": goal}]
        history: list[dict[str, Any]] = []
        repeated_calls: Counter[str] = Counter()

        for step in range(1, self.max_steps + 1):
            self._log(f"[agent {step}/{self.max_steps}] 请求模型决策")
            turn = await self.model.complete(
                input_items=input_items,
                tools=self.tools.schemas(),
                instructions=SYSTEM_PROMPT,
            )
            input_items.extend(turn.output_items)

            if not turn.tool_calls:
                final_text = turn.text.strip() or "Agent 已停止，但没有返回说明。"
                self._log(f"[agent] {final_text}")
                return AgentResult(text=final_text, steps=step, tool_calls=history)

            for call in turn.tool_calls:
                signature = f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                repeated_calls[signature] += 1
                if repeated_calls[signature] > 3:
                    result: Any = {
                        "ok": False,
                        "error": "同一个工具调用已重复三次，请改变策略或结束任务",
                    }
                else:
                    self._log(f"[tool] {call.name} {json.dumps(call.arguments, ensure_ascii=False)}")
                    result = await self.tools.execute(call.name, call.arguments)

                history.append({"name": call.name, "arguments": call.arguments, "result": result})
                rendered = json.dumps(result, ensure_ascii=False, default=str)
                self._log(f"[result] {rendered[:800]}")
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": rendered,
                    }
                )

        raise RuntimeError(f"Agent 超过最大工具调用轮数 {self.max_steps}，已安全停止")
