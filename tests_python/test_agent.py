from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from formpilot.agent import FormPilotAgent
from formpilot.cli import compose_goal
from formpilot.config import load_env_file
from formpilot.logging_util import RunLogger
from formpilot.model import (
    ModelTurn,
    OpenAIChatCompletionsModel,
    ToolCall,
    create_model,
    resolve_api_mode,
)
from formpilot.credentials import match_project_option, match_select_option
from formpilot.policy import ApprovalPolicy, page_requires_user_pause
from formpilot.profile import ProfileStore
from formpilot.task import TaskBrief
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

    async def search_visible_text(self, query, *, limit=20):
        haystacks = [
            "网上报名",
            "博士研究生招生",
            "信息填报",
            "下一步",
        ]
        needle = str(query).lower()
        matches = [{"text": text, "tag": "a", "href": "/apply"} for text in haystacks if needle in text.lower()]
        return {"ok": True, "query": query, "matches": matches[:limit]}

    async def capture_captcha_image(self, field_id):
        # Deterministic fake captcha bytes for unit tests; OCR is mocked in dedicated tests.
        return {"ok": True, "image_id": "captcha-1", "tag": "img", "png": b"fake-captcha-png"}


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

    async def test_agent_forces_pause_instead_of_exiting_on_login_page(self):
        registry = ToolRegistry()
        pauses: list[str] = []

        async def inspect_page():
            return {
                "ok": True,
                "title": "研究生报考服务系统",
                "url": "https://yzbm.tongji.edu.cn/logon",
                "fields": [
                    {"field_id": "field-user", "label": "用户名", "type": "text", "has_value": False},
                    {"field_id": "field-pass", "label": "密码", "type": "password", "has_value": False},
                    {"field_id": "field-captcha", "label": "验证码", "type": "text", "has_value": False},
                ],
                "controls": [{"control_id": "login", "label": "登录"}],
            }

        async def pause_for_user(message: str):
            pauses.append(message)
            return {
                "ok": True,
                "message": "用户已接管并返回",
                "title": "报名表",
                "url": "https://yzbm.tongji.edu.cn/apply",
                "fields": [{"field_id": "field-name", "label": "姓名", "type": "text", "has_value": False}],
                "controls": [{"control_id": "next", "label": "下一步"}],
            }

        registry.register(Tool("inspect_page", "inspect", {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, inspect_page))
        registry.register(
            Tool(
                "pause_for_user",
                "pause",
                {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                pause_for_user,
            )
        )
        model = ScriptedModel([
            ModelTurn(output_items=[{"type": "function_call", "call_id": "c1"}], tool_calls=[ToolCall("c1", "inspect_page", {})]),
            ModelTurn(output_items=[{"type": "message"}], text="请先登录后再继续。"),
            ModelTurn(output_items=[{"type": "message"}], text="登录后已可继续填写，姓名待填，尚未提交。"),
        ])
        result = await FormPilotAgent(model, registry, max_steps=5).run("填写报名表")
        self.assertEqual(len(pauses), 1)
        self.assertIn("登录", pauses[0])
        self.assertEqual(result.steps, 3)
        self.assertIn("尚未提交", result.text)
        self.assertTrue(any(item["name"] == "pause_for_user" for item in result.tool_calls))

    async def test_pause_user_guidance_is_injected_into_conversation(self):
        registry = ToolRegistry()

        async def pause_for_user(message: str):
            return {
                "ok": True,
                "message": "用户已接管并返回",
                "url": "https://yzbm.tongji.edu.cn/sstm/tm/index",
                "fields": [],
                "controls": [{"control_id": "control-1", "label": "网上报名", "type": "link"}],
                "user_guidance": "进入网上报名后选择博士信息填报",
            }

        async def click_control(control_id, approval_id=None):
            return {"ok": True, "label": "网上报名"}

        registry.register(
            Tool(
                "pause_for_user",
                "pause",
                {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                    "additionalProperties": False,
                },
                pause_for_user,
            )
        )
        registry.register(
            Tool(
                "click_control",
                "click",
                {
                    "type": "object",
                    "properties": {
                        "control_id": {"type": "string"},
                        "approval_id": {"type": ["string", "null"]},
                    },
                    "required": ["control_id", "approval_id"],
                    "additionalProperties": False,
                },
                click_control,
            )
        )
        model = ScriptedModel([
            ModelTurn(
                output_items=[{"type": "function_call", "call_id": "c1"}],
                tool_calls=[ToolCall("c1", "pause_for_user", {"message": "请登录后继续"})],
            ),
            ModelTurn(
                output_items=[{"type": "function_call", "call_id": "c2"}],
                tool_calls=[ToolCall("c2", "click_control", {"control_id": "control-1", "approval_id": None})],
            ),
            ModelTurn(output_items=[{"type": "message"}], text="已按指引进入网上报名，尚未提交。"),
        ])
        result = await FormPilotAgent(model, registry, max_steps=5).run("填写报名表")
        self.assertEqual(result.steps, 3)
        guidance_msgs = [
            item for item in model.inputs[1]
            if isinstance(item, dict) and item.get("role") == "user" and "用户补充指引" in str(item.get("content", ""))
        ]
        self.assertTrue(guidance_msgs)
        self.assertIn("博士信息填报", guidance_msgs[0]["content"])


class EnvironmentTests(unittest.TestCase):
    def test_compose_goal_appends_guidance(self):
        text = compose_goal("填写报名表", ["报考博士", "从网上报名进入"], task_path="task.md")
        self.assertIn("填写报名表", text)
        self.assertIn("用户补充指引", text)
        self.assertIn("报考博士", text)
        self.assertIn("从网上报名进入", text)
        self.assertIn("任务说明", text)

    def test_task_brief_parses_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text(
                "# 任务\n\n## 已知事实\n\n- application.mode: 普通招考\n\n```json\n{\"identity.ethnicity\": \"汉族\"}\n```\n",
                encoding="utf-8",
            )
            brief = TaskBrief.load(path)
            self.assertEqual(brief.get_fact("application.mode"), "普通招考")
            self.assertEqual(brief.get_fact("identity.ethnicity"), "汉族")
            view = brief.public_view()
            self.assertIn("普通招考", view["content"])

    def test_run_logger_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = RunLogger(directory, also_print=False)
            logger.tool(step=1, name="inspect_page", arguments={}, result={"ok": True, "url": "https://example.com", "fields": []})
            logger.close()
            lines = logger.path.read_text(encoding="utf-8").strip().splitlines()
            self.assertGreaterEqual(len(lines), 3)
            payload = json.loads(lines[-2])
            self.assertEqual(payload["type"], "tool")
            self.assertEqual(payload["name"], "inspect_page")

    def test_login_page_requires_user_pause(self):
        self.assertTrue(
            page_requires_user_pause(
                {
                    "url": "https://yzbm.tongji.edu.cn/logon",
                    "fields": [{"label": "密码", "type": "password", "has_value": False}],
                    "controls": [{"label": "登录"}],
                }
            )
        )
        self.assertFalse(
            page_requires_user_pause(
                {
                    "url": "https://yzbm.tongji.edu.cn/apply",
                    "fields": [{"label": "姓名", "type": "text", "has_value": False}],
                    "controls": [{"label": "下一步"}],
                }
            )
        )

    def test_select_match_rejects_short_ambiguous_token(self):
        options = [
            {"text": "博士研究生招生", "value": "3"},
            {"text": "硕士研究生招生", "value": "2"},
            {"text": "推免生预报名", "value": "5"},
        ]
        self.assertIsNone(match_select_option(options, "研究生"))
        self.assertEqual(match_select_option(options, "推免生预报名")["value"], "5")
        self.assertEqual(match_project_option(options, "我要报考推免生预报名")["value"], "5")
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

    async def test_profile_catalog_includes_values_for_reasoning(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.get_profile_catalog()
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertIn("identity.full_name_zh", rendered)
        self.assertIn("张三", rendered)
        self.assertIn("310101199001011234", rendered)
        profile = await tools.read_profile()
        self.assertEqual(profile["profile"]["identity"]["full_name_zh"], "张三")

    async def test_secret_field_is_blocked_even_when_model_requests_it(self):
        tools = FormPilotTools(self.browser, self.profile)
        result = await tools.fill_from_profile("field-captcha", "identity.full_name_zh", None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(self.browser.values["field-captcha"], "9876")

    async def test_profile_and_inferred_fill_without_extra_approval(self):
        tools = FormPilotTools(self.browser, self.profile)
        filled = await tools.fill_from_profile("field-id", "identity.document_number", None)
        self.assertTrue(filled["ok"])
        self.assertEqual(self.browser.values["field-id"], "310101199001011234")
        inferred = await tools.fill_text("field-name", "张三", reason="资料姓名")
        self.assertTrue(inferred["ok"])
        self.assertEqual(self.browser.values["field-name"], "张三")
        blocked = await tools.fill_text("field-captcha", "ABCD", reason="should block")
        self.assertTrue(blocked.get("blocked"))

    async def test_next_step_blocked_when_required_empty(self):
        class RequiredBrowser(FakeBrowser):
            def snapshot(self):
                data = super().snapshot()
                data["fields"] = [
                    {
                        "field_id": "field-name",
                        "tag": "input",
                        "type": "text",
                        "id": "xm",
                        "name": "xm",
                        "label": "姓名",
                        "placeholder": "",
                        "required": True,
                        "disabled": False,
                        "read_only": False,
                        "current_value": "",
                        "has_value": False,
                        "options": None,
                    }
                ]
                data["controls"] = [{"control_id": "next", "label": "下一步", "type": "button", "disabled": False}]
                return data

        tools = FormPilotTools(RequiredBrowser(), self.profile)
        result = await tools.click_control("next", None)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["incomplete_required_count"] if "incomplete_required_count" in result else len(result["incomplete_required"]), 1)

    async def test_request_missing_profile_fields_writes_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_path = root / "profile.json"
            template_path = root / "profile.json.template"
            profile_path.write_text('{"identity":{"full_name_zh":"张三"}}\n', encoding="utf-8")
            template_path.write_text('{"identity":{"full_name_zh":"示例"}}\n', encoding="utf-8")
            store = ProfileStore.load(profile_path, template_path=template_path)
            tools = FormPilotTools(self.browser, store, profile_path=profile_path, template_path=template_path)
            with mock.patch("builtins.input", side_effect=["共青团员", ""]):
                result = await tools.request_missing_profile_fields(
                    [
                        {
                            "label": "政治面貌",
                            "profile_path": "identity.political_status",
                            "hint": "请填写政治面貌",
                        },
                        {
                            "label": "跳过项",
                            "profile_path": "identity.skip_me",
                            "hint": "可跳过",
                        },
                    ]
                )
            self.assertTrue(result["ok"])
            self.assertIn("identity.political_status", result["updated_paths"])
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["identity"]["political_status"], "共青团员")
            template = json.loads(template_path.read_text(encoding="utf-8"))
            self.assertEqual(template["identity"]["political_status"], "共青团员")

    async def test_submit_click_needs_explicit_approval(self):
        tools = FormPilotTools(self.browser, self.profile, confirm=lambda _: True)
        first = await tools.click_control("submit", None)
        self.assertTrue(first["confirmation_required"])
        approval = await tools.request_user_confirmation(first["target"], first["summary"])
        clicked = await tools.click_control("submit", approval["approval_id"])
        self.assertTrue(clicked["ok"])
        self.assertEqual(self.browser.clicked, ["submit"])

    async def test_cascade_from_profile_fills_without_extra_approval(self):
        tools = FormPilotTools(self.browser, self.profile)
        paths = ["origin.province", "origin.city", "origin.district"]
        result = await tools.select_cascade_from_profile("field-origin", paths, None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.browser.last_cascade, ["浙江省", "温州市", "瑞安市"])
        self.assertNotIn("confirmation_required", result)

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
            "read_task_brief", "read_profile", "search_visible_text", "fill_from_task_fact", "fill_text",
            "request_missing_profile_fields", "dismiss_page_overlays", "attempt_auto_login",
        }.issubset(names))

    async def test_fill_from_task_fact_uses_local_value(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.md"
            path.write_text("## 已知事实\n\n- identity.ethnicity: 汉族\n", encoding="utf-8")
            tools = FormPilotTools(self.browser, self.profile, task=TaskBrief.load(path))
            result = await tools.fill_from_task_fact("field-name", "identity.ethnicity")
            self.assertTrue(result["ok"])
            self.assertEqual(self.browser.values["field-name"], "汉族")
            if result.get("verification"):
                self.assertNotIn("actual", result["verification"])

    async def test_attempt_auto_login_selects_project_and_fills_captcha(self):
        old_user = os.environ.get("FORMPILOT_LOGIN_USERNAME")
        old_pass = os.environ.get("FORMPILOT_LOGIN_PASSWORD")
        try:
            os.environ["FORMPILOT_LOGIN_USERNAME"] = "demo-user"
            os.environ["FORMPILOT_LOGIN_PASSWORD"] = "demo-pass"

            class LoginBrowser(FakeBrowser):
                def __init__(self):
                    super().__init__()
                    self.logged_in = False

                def snapshot(self):
                    if self.logged_in:
                        return {
                            "title": "首页",
                            "url": "https://yzbm.tongji.edu.cn/home",
                            "fields": [],
                            "controls": [],
                            "feedback": [],
                        }
                    return {
                        "title": "登录",
                        "url": "https://yzbm.tongji.edu.cn/logon",
                        "fields": [
                            {
                                "field_id": "field-project",
                                "tag": "select",
                                "type": "select",
                                "id": "kslb",
                                "name": "kslb",
                                "label": "招生项目",
                                "placeholder": "",
                                "disabled": False,
                                "read_only": False,
                                "current_value": self.values.get("field-project", "3"),
                                "options": [
                                    {"text": "博士研究生招生", "value": "3", "selected": self.values.get("field-project", "3") == "3", "disabled": False},
                                    {"text": "推免生预报名", "value": "5", "selected": self.values.get("field-project") == "5", "disabled": False},
                                ],
                            },
                            {"field_id": "field-user", "tag": "input", "type": "text", "id": "username", "name": "username", "label": "用户名", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values.get("field-user", ""), "options": None},
                            {"field_id": "field-pass", "tag": "input", "type": "password", "id": "password", "name": "password", "label": "密码", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values.get("field-pass", ""), "options": None},
                            {"field_id": "field-captcha", "tag": "input", "type": "text", "id": "captcha", "name": "captcha", "label": "验证码", "placeholder": "", "disabled": False, "read_only": False, "current_value": self.values.get("field-captcha", ""), "options": None},
                        ],
                        "controls": [{"control_id": "login", "label": "登录", "type": "button", "disabled": False}],
                        "feedback": [],
                    }

                async def fill(self, field_id, value):
                    if field_id == "field-project":
                        self.values[field_id] = "5" if "推免" in str(value) else str(value)
                        return {"ok": True, "field_id": field_id, "verification": {"matches": True}}
                    return await super().fill(field_id, value)

                async def click(self, control_id):
                    self.clicked.append(control_id)
                    if control_id == "login" and self.values.get("field-captcha") == "AB12":
                        self.logged_in = True
                    return {"ok": True, "label": "登录"}

            browser = LoginBrowser()
            browser.values = {"field-project": "3", "field-user": "", "field-pass": "", "field-captcha": ""}
            pauses: list[str] = []
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "task.md"
                path.write_text("我要报考推免生预报名。\n", encoding="utf-8")
                tools = FormPilotTools(
                    browser,
                    self.profile,
                    task=TaskBrief.load(path),
                    pause=lambda message: pauses.append(message) or "",
                )
                with mock.patch(
                    "formpilot.captcha.recognize_captcha_image",
                    new=mock.AsyncMock(return_value={"text": "AB12", "backend": "llm", "error": ""}),
                ):
                    result = await tools.attempt_auto_login()
            self.assertIn("username", result["filled"])
            self.assertIn("password", result["filled"])
            self.assertIn("captcha", result["filled"])
            self.assertTrue(result.get("clicked_login"))
            self.assertIn("login_click", result["filled"])
            self.assertTrue(result.get("ok"))
            self.assertEqual(browser.values["field-user"], "demo-user")
            self.assertEqual(browser.values["field-pass"], "demo-pass")
            self.assertEqual(browser.values["field-captcha"], "AB12")
            self.assertEqual(result.get("selected_project"), "推免生预报名")
            self.assertFalse(pauses)
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("demo-pass", rendered)
            self.assertNotIn("demo-user", rendered)
            self.assertNotIn("AB12", rendered)
        finally:
            if old_user is None:
                os.environ.pop("FORMPILOT_LOGIN_USERNAME", None)
            else:
                os.environ["FORMPILOT_LOGIN_USERNAME"] = old_user
            if old_pass is None:
                os.environ.pop("FORMPILOT_LOGIN_PASSWORD", None)
            else:
                os.environ["FORMPILOT_LOGIN_PASSWORD"] = old_pass


class VisionCaptchaSettingsTests(unittest.TestCase):
    def test_vision_settings_are_independent_of_main_model(self):
        from formpilot.captcha import resolve_vision_settings

        old = {
            key: os.environ.get(key)
            for key in (
                "FORMPILOT_MODEL",
                "FORMPILOT_VISION_MODEL",
                "FORMPILOT_VISION_BASE_URL",
                "FORMPILOT_VISION_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_API_KEY",
                "DASHSCOPE_API_KEY",
            )
        }
        try:
            os.environ["FORMPILOT_MODEL"] = "deepseek-v4-flash"
            os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com"
            os.environ["OPENAI_API_KEY"] = "main-key"
            os.environ["FORMPILOT_VISION_MODEL"] = "qwen3-vl-plus"
            os.environ["FORMPILOT_VISION_BASE_URL"] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
            os.environ["FORMPILOT_VISION_API_KEY"] = "vision-key"
            settings = resolve_vision_settings()
            self.assertEqual(settings["model"], "qwen3-vl-plus")
            self.assertEqual(settings["base_url"], "https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.assertEqual(settings["api_key"], "vision-key")
            self.assertNotEqual(settings["model"], os.environ["FORMPILOT_MODEL"])
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_vision_rejects_deepseek_text_only_endpoint(self):
        from formpilot.captcha import _is_text_only_endpoint

        self.assertTrue(_is_text_only_endpoint("https://api.deepseek.com", "qwen3-vl-plus"))
        self.assertTrue(_is_text_only_endpoint("", "deepseek-v4-pro"))
        self.assertFalse(
            _is_text_only_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-vl-plus")
        )

    def test_extract_captcha_ignores_chinese_explanations(self):
        from formpilot.captcha import _extract_captcha_code

        self.assertEqual(_extract_captcha_code("无法识别图片为空白"), "")
        self.assertEqual(_extract_captcha_code("EMPTY"), "")
        self.assertEqual(_extract_captcha_code("验证码是 AB12"), "AB12")
        self.assertEqual(_extract_captcha_code("ab12"), "ab12")


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