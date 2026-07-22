from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from formpilot.agent import FormPilotAgent
from formpilot.config import load_env_file
from formpilot.model import (
    ModelTurn,
    OpenAIChatCompletionsModel,
    ToolCall,
    create_model,
    resolve_api_mode,
)
from formpilot.policy import ApprovalPolicy
from formpilot.profile import ProfileStore
from formpilot.tools.base import Tool, ToolRegistry
from formpilot.tools.form_tools import FormPilotTools


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = turns
        self.inputs: list[list[Any]] = []

    async def complete(self, *, input_items, tools, instructions):
        self.inputs.append(list(input_items))
        return self.turns.pop(0)


class _FakeFunction:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, *, content=None, reasoning_content=None, tool_calls=None) -> None:
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeResponse:
    def __init__(self, message: _FakeMessage) -> None:
        self.choices = [_FakeChoice(message)]


class ScriptedChatClient:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)
        self.calls: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._messages.pop(0))


class FakeBrowser:
    def __init__(self) -> None:
        self.last_snapshot = None
        self.values = {
            "field-name": "",
            "field-id": "310101199001011234",
            "field-captcha": "9876",
            "field-origin": "",
            "field-date": "",
        }
        self.clicked: list[str] = []
        self.last_cascade = None
        self.last_date = None

    def snapshot(self):
        return {
            "title": "报名系统",
            "url": "https://example.edu/apply",
            "fields": [
                {"field_id": "field-name", "tag": "input", "type": "text", "id": "xm", "name": "xm", "label": "姓名", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values["field-name"], "options": None},
                {"field_id": "field-id", "tag": "input", "type": "text", "id": "zjhm", "name": "zjhm", "label": "证件号码", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values["field-id"], "options": None},
                {"field_id": "field-captcha", "tag": "input", "type": "text", "id": "captcha", "name": "captcha", "label": "验证码", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values["field-captcha"], "options": None},
                {"field_id": "field-origin", "tag": "input", "type": "text", "id": "origin", "name": "origin", "label": "籍贯", "placeholder": "", "disabled": False, "read_only": True, "current_value": self.values["field-origin"], "options": None},
                {"field_id": "field-date", "tag": "input", "type": "text", "id": "enrollment", "name": "enrollment", "label": "入学时间", "placeholder": "", "disabled": False, "read_only": True, "current_value": self.values["field-date"], "options": None},
            ],
            "controls": [{"control_id": "submit", "label": "提交报名", "type": "submit", "disabled": False}],
            "feedback": [],
        }

    async def inspect(self):
        self.last_snapshot = self.snapshot()
        return self.last_snapshot

    async def wait_and_rescan(self, milliseconds=800):
        return await self.inspect()

    async def fill(self, field_id, value):
        self.values[field_id] = str(value)
        return {"ok": True, "field_id": field_id, "verification": {"matches": True, "actual": str(value)}}

    async def verify(self, field_id, expected=None):
        actual = self.values[field_id]
        return {"ok": True, "field_id": field_id, "actual": actual, "matches": expected is None or actual == expected}

    async def click(self, control_id):
        self.clicked.append(control_id)
        return {"ok": True, "label": "提交报名"}

    async def inspect_widget(self):
        return {"widgets": [], "options": [], "controls": []}

    async def open_field(self, field_id):
        return {"ok": True, "field_id": field_id, "widgets": [], "options": [], "controls": []}

    async def click_widget_option(self, option_id):
        return {"ok": True, "option_id": option_id}

    async def click_widget_control(self, widget_control_id):
        return {"ok": True, "widget_control_id": widget_control_id}

    async def select_cascade(self, field_id, path):
        self.last_cascade = list(path)
        self.values[field_id] = "/".join(path)
        return {"ok": True, "field_id": field_id, "selected_levels": len(path), "has_value": True}

    async def set_date(self, field_id, value):
        self.last_date = value
        self.values[field_id] = str(value)
        return {"ok": True, "field_id": field_id, "mode": "calendar", "has_value": True}


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_autonomously_calls_tools_until_final_text(self):
        registry = ToolRegistry()
        events = []

        async def inspect_page():
            events.append("inspect")
            return {"ok": True, "fields": [{"field_id": "f1", "label": "姓名"}]}

        async def fill(field_id, profile_path):
            events.append((field_id, profile_path))
            return {"ok": True}

        registry.register(Tool("inspect_page", "inspect", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, inspect_page))
        registry.register(Tool("fill", "fill", {"type": "object", "properties": {"field_id": {"type": "string"}, "profile_path": {"type": "string"}}, "required": ["field_id", "profile_path"], "additionalProperties": False}, fill))
        model = ScriptedModel([
            ModelTurn(output_items=[{"type": "function_call", "call_id": "c1"}], tool_calls=[ToolCall("c1", "inspect_page", {})]),
            ModelTurn(output_items=[{"type": "function_call", "call_id": "c2"}], tool_calls=[ToolCall("c2", "fill", {"field_id": "f1", "profile_path": "identity.full_name_zh"})]),
            ModelTurn(output_items=[{"type": "message"}], text="姓名已填写，尚未提交。"),
        ])
        result = await FormPilotAgent(model, registry, max_steps=5).run("填写报名表")
        self.assertEqual(events, ["inspect", ("f1", "identity.full_name_zh")])
        self.assertEqual(result.steps, 3)
        self.assertIn("尚未提交", result.text)
        tool_outputs = [item for item in model.inputs[-1] if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.assertEqual(json.loads(tool_outputs[-1]["output"]), {"ok": True})


class EnvironmentTests(unittest.TestCase):
    def test_load_env_file_preserves_existing_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("FORMPILOT_TEST_VALUE=file\nFORMPILOT_QUOTED='hello world'\n", encoding="utf-8")
            old_value = os.environ.get("FORMPILOT_TEST_VALUE")
            old_quoted = os.environ.get("FORMPILOT_QUOTED")
            try:
                os.environ["FORMPILOT_TEST_VALUE"] = "process"
                os.environ.pop("FORMPILOT_QUOTED", None)
                self.assertTrue(load_env_file(path))
                self.assertEqual(os.environ["FORMPILOT_TEST_VALUE"], "process")
                self.assertEqual(os.environ["FORMPILOT_QUOTED"], "hello world")
            finally:
                if old_value is None:
                    os.environ.pop("FORMPILOT_TEST_VALUE", None)
                else:
                    os.environ["FORMPILOT_TEST_VALUE"] = old_value
                if old_quoted is None:
                    os.environ.pop("FORMPILOT_QUOTED", None)
                else:
                    os.environ["FORMPILOT_QUOTED"] = old_quoted

    def test_invalid_env_line_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("NOT_VALID\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected KEY=value"):
                load_env_file(path)


class FormToolSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = FakeBrowser()
        self.profile = ProfileStore({
            "identity": {"full_name_zh": "张三", "document_number": "310101199001011234"},
            "contact": {"mobile": "13800138000"},
            "education": {"enrollment_date": "2022-03-01"},
            "origin": {"province": "浙江省", "city": "温州市", "district": "瑞安市"},
        })

    async def test_inspection_never_exposes_existing_values(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.inspect_page()
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("310101199001011234", rendered)
        self.assertNotIn("9876", rendered)
        self.assertTrue(result["fields"][1]["has_value"])

    async def test_profile_catalog_contains_semantics_but_no_values(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.get_profile_catalog()
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("identity.full_name_zh", rendered)
        self.assertNotIn("张三", rendered)
        self.assertNotIn("310101199001011234", rendered)

    async def test_secret_field_is_blocked_even_when_model_requests_it(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.fill_from_profile("field-captcha", "identity.full_name_zh", None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(self.browser.values["field-captcha"], "9876")

    async def test_high_risk_fill_needs_single_use_approval(self):
        policy = ApprovalPolicy()
        tools = FormPilotTools(self.browser, self.profile, policy=policy, confirm=lambda _: True)
        first = await tools.fill_from_profile("field-id", "identity.document_number", None)
        self.assertTrue(first["confirmation_required"])
        approval = await tools.request_user_confirmation(first["target"], first["summary"])
        filled = await tools.fill_from_profile("field-id", "identity.document_number", approval["approval_id"])
        self.assertTrue(filled["ok"])
        self.assertNotIn("actual", filled["verification"])
        replay = await tools.fill_from_profile("field-id", "identity.document_number", approval["approval_id"])
        self.assertTrue(replay["confirmation_required"])

    async def test_submit_click_needs_explicit_approval(self):
        tools = FormPilotTools(self.browser, self.profile, confirm=lambda _: True)
        first = await tools.click_control("submit", None)
        self.assertTrue(first["confirmation_required"])
        approval = await tools.request_user_confirmation(first["target"], first["summary"])
        clicked = await tools.click_control("submit", approval["approval_id"])
        self.assertTrue(clicked["ok"])
        self.assertEqual(self.browser.clicked, ["submit"])

    async def test_cascade_values_stay_local_and_require_location_approval(self):
        tools = FormPilotTools(self.browser, self.profile, confirm=lambda _: True)
        paths = ["origin.province", "origin.city", "origin.district"]
        first = await tools.select_cascade_from_profile("field-origin", paths, None)
        self.assertTrue(first["confirmation_required"])
        approval = await tools.request_user_confirmation(first["target"], first["summary"])
        result = await tools.select_cascade_from_profile("field-origin", paths, approval["approval_id"])
        self.assertTrue(result["ok"])
        self.assertEqual(self.browser.last_cascade, ["浙江省", "温州市", "瑞安市"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("浙江省", rendered)
        self.assertNotIn("温州市", rendered)
        self.assertNotIn("瑞安市", rendered)

    async def test_custom_date_uses_local_profile_value(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.set_date_from_profile("field-date", "education.enrollment_date", None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.browser.last_date, "2022-03-01")
        self.assertNotIn("2022-03-01", json.dumps(result, ensure_ascii=False))

    async def test_registry_exposes_complex_widget_tools(self):
        tools = FormPilotTools(self.browser, self.profile)
        names = {schema["name"] for schema in tools.registry().schemas()}
        self.assertTrue({
            "open_field", "inspect_widget", "click_widget_option", "click_widget_control",
            "select_cascade_from_profile", "set_date_from_profile",
        }.issubset(names))


class DeepSeekChatCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_auto_mode_selects_chat_for_deepseek(self):
        self.assertEqual(resolve_api_mode("deepseek-v4-flash"), "chat")
        self.assertEqual(
            resolve_api_mode("gpt-5.6-terra", base_url="https://api.deepseek.com"),
            "chat",
        )
        self.assertEqual(resolve_api_mode("gpt-5.6-terra"), "responses")
        self.assertEqual(resolve_api_mode("deepseek-v4-pro", api_mode="responses"), "responses")
        self.assertIsInstance(create_model("deepseek-v4-flash", client=object()), OpenAIChatCompletionsModel)

    async def test_chat_adapter_echoes_reasoning_content_across_tool_turns(self):
        client = ScriptedChatClient(
            [
                _FakeMessage(
                    content="",
                    reasoning_content="先检查页面字段",
                    tool_calls=[_FakeToolCall("call_1", "inspect_page", "{}")],
                ),
                _FakeMessage(content="页面已检查，尚未提交。", reasoning_content="可以结束"),
            ]
        )
        model = OpenAIChatCompletionsModel("deepseek-v4-flash", "medium", client=client)

        registry = ToolRegistry()

        async def inspect_page():
            return {"ok": True, "fields": []}

        registry.register(
            Tool(
                "inspect_page",
                "inspect",
                {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                inspect_page,
            )
        )

        result = await FormPilotAgent(model, registry, max_steps=5).run("填写报名表")
        self.assertEqual(result.steps, 2)
        self.assertIn("尚未提交", result.text)
        self.assertEqual(len(client.calls), 2)

        first = client.calls[0]
        self.assertEqual(first["model"], "deepseek-v4-flash")
        self.assertEqual(first["reasoning_effort"], "high")
        self.assertEqual(first["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(first["tools"][0]["function"]["name"], "inspect_page")

        second_messages = client.calls[1]["messages"]
        assistant = next(item for item in second_messages if item.get("role") == "assistant")
        self.assertEqual(assistant["reasoning_content"], "先检查页面字段")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        tool_msg = next(item for item in second_messages if item.get("role") == "tool")
        self.assertEqual(tool_msg["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tool_msg["content"]), {"ok": True, "fields": []})


if __name__ == "__main__":
    unittest.main()