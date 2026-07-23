from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from .model import AgentModel
from .policy import page_requires_user_pause
from .prompts import SYSTEM_PROMPT
from .tools import ToolRegistry


TraceHandler = Callable[[str], None]


@dataclass(slots=True)
class AgentResult:
    text: str
    steps: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    log_path: str | None = None


class FormPilotAgent:
    def __init__(
        self,
        model: AgentModel,
        tools: ToolRegistry,
        *,
        max_steps: int = 80,
        trace: TraceHandler | None = None,
        run_logger: Any | None = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.max_steps = max_steps
        self.trace = trace
        self.run_logger = run_logger

    def _log(self, message: str) -> None:
        if self.trace:
            self.trace(message)
        elif self.run_logger is not None:
            # RunLogger is also a TraceHandler; prefer explicit trace when provided.
            self.run_logger.event("trace", message=message)

    @staticmethod
    def _latest_page_snapshot(history: list[dict[str, Any]]) -> dict[str, Any] | None:
        for item in reversed(history):
            if item.get("name") not in {"inspect_page", "wait_and_rescan", "pause_for_user"}:
                continue
            result = item.get("result")
            if isinstance(result, dict) and result.get("ok") and ("url" in result or "fields" in result):
                return result
        return None

    async def _force_pause_for_login(
        self,
        *,
        message: str,
        step: int,
        input_items: list[Any],
        history: list[dict[str, Any]],
    ) -> None:
        auto = await self.tools.execute("attempt_auto_login", {})
        if isinstance(auto, dict):
            history.append({"name": "attempt_auto_login", "arguments": {}, "result": auto})
            if self.run_logger is not None:
                self.run_logger.tool(step=step, name="attempt_auto_login", arguments={}, result=auto)
            rendered_auto = json.dumps(auto, ensure_ascii=False, default=str)
            self._log(f"[tool] attempt_auto_login {{}}")
            self._log(f"[result] {rendered_auto[:800]}")
            input_items.append(
                {
                    "role": "user",
                    "content": (
                        "系统已尝试自动登录（按任务选择招生项目、填账号密码、识别图形验证码并尽量点击登录；原值未暴露）。"
                        f"工具结果：{rendered_auto}\n"
                        "若仍停在登录页且 needs_human 含短信验证码，再 pause；否则继续 inspect_page 填表。"
                    ),
                }
            )
            needs_human = auto.get("needs_human") or []
            if auto.get("ok") and not needs_human:
                # Left login or finished autofill without SMS leftovers.
                latest = auto if isinstance(auto, dict) else None
                if latest and not page_requires_user_pause(latest):
                    return
                if auto.get("clicked_login") and not needs_human:
                    # Captcha filled and login clicked; even if URL briefly unchanged, continue.
                    return

        pause_message = message.strip()
        if isinstance(auto, dict) and auto.get("filled"):
            human_bits = "、".join(str(item) for item in (auto.get("needs_human") or ["短信验证码"]))
            pause_message = (
                f"自动登录已尽量完成。请在浏览器中处理{human_bits}（如有），完成后回到终端按 Enter。"
            )
        elif not pause_message:
            pause_message = "请在浏览器中完成短信验证码等必须人工的步骤，完成后回到终端按 Enter。"

        self._log("[agent] 仍需人工短信验证码/确认，pause_for_user 等待你完成后继续")
        result = await self.tools.execute("pause_for_user", {"message": pause_message})
        history.append({"name": "pause_for_user", "arguments": {"message": pause_message}, "result": result})
        if self.run_logger is not None:
            self.run_logger.tool(step=step, name="pause_for_user", arguments={"message": pause_message}, result=result)
        rendered = json.dumps(result, ensure_ascii=False, default=str)
        self._log(f"[result] {rendered[:800]}")
        self._append_pause_followup(input_items, result)

    @staticmethod
    def _append_pause_followup(input_items: list[Any], result: Any) -> None:
        rendered = json.dumps(result, ensure_ascii=False, default=str)
        guidance = ""
        if isinstance(result, dict) and result.get("user_guidance"):
            guidance = f"\n用户补充指引：{result['user_guidance']}\n请优先遵循该指引自主选择入口并继续操作。"
        input_items.append(
            {
                "role": "user",
                "content": (
                    "系统已代为调用 pause_for_user，用户已在浏览器处理并返回。"
                    f"工具结果：{rendered}"
                    f"{guidance}\n"
                    "请立刻再次 inspect_page，根据任务文档与可见控件自主导航或填写；"
                    "不要再次仅用文字结束，也不要把普通选择推回给用户。"
                ),
            }
        )

    async def run(self, goal: str) -> AgentResult:
        input_items: list[Any] = [{"role": "user", "content": goal}]
        history: list[dict[str, Any]] = []
        repeated_calls: Counter[str] = Counter()
        if self.run_logger is not None:
            self.run_logger.event("goal", goal=goal)

        for step in range(1, self.max_steps + 1):
            self._log(f"[agent {step}/{self.max_steps}] 请求模型决策")
            if self.run_logger is not None:
                self.run_logger.event("decision_requested", step=step)
            turn = await self.model.complete(
                input_items=input_items,
                tools=self.tools.schemas(),
                instructions=SYSTEM_PROMPT,
            )
            input_items.extend(turn.output_items)

            if not turn.tool_calls:
                snapshot = self._latest_page_snapshot(history)
                if snapshot is not None and page_requires_user_pause(snapshot):
                    await self._force_pause_for_login(
                        message=turn.text,
                        step=step,
                        input_items=input_items,
                        history=history,
                    )
                    continue

                final_text = turn.text.strip() or "Agent 已停止，但没有返回说明。"
                self._log(f"[agent] {final_text}")
                if self.run_logger is not None:
                    self.run_logger.event("final", step=step, text=final_text)
                return AgentResult(
                    text=final_text,
                    steps=step,
                    tool_calls=history,
                    log_path=str(self.run_logger.path) if self.run_logger is not None else None,
                )

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
                if self.run_logger is not None:
                    self.run_logger.tool(step=step, name=call.name, arguments=call.arguments, result=result)
                rendered = json.dumps(result, ensure_ascii=False, default=str)
                self._log(f"[result] {rendered[:800]}")
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": rendered,
                    }
                )
                if call.name == "pause_for_user" and isinstance(result, dict) and result.get("user_guidance"):
                    input_items.append(
                        {
                            "role": "user",
                            "content": (
                                f"用户补充指引：{result['user_guidance']}\n"
                                "请按该指引继续自主决策与操作，不要把导航选择再推回给用户。"
                            ),
                        }
                    )

        self._log(f"Agent 超过最大工具调用轮数 {self.max_steps}，已安全停止")
        message = f"Agent 超过最大工具调用轮数 {self.max_steps}，已安全停止（未崩溃）。可提高 FORMPILOT_MAX_STEPS 后继续。"
        if self.run_logger is not None:
            self.run_logger.event("max_steps", max_steps=self.max_steps)
        return AgentResult(
            text=message,
            steps=self.max_steps,
            tool_calls=history,
            log_path=str(self.run_logger.path) if self.run_logger is not None else None,
        )
