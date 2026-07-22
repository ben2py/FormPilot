from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Protocol

from ..policy import ApprovalPolicy
from ..profile import HIGH_RISK_PATHS, ProfileStore
from .base import Tool, ToolRegistry


class BrowserLike(Protocol):
    last_snapshot: dict[str, Any] | None

    async def inspect(self) -> dict[str, Any]: ...
    async def wait_and_rescan(self, milliseconds: int = 800) -> dict[str, Any]: ...
    async def fill(self, field_id: str, value: Any) -> dict[str, Any]: ...
    async def verify(self, field_id: str, expected: Any | None = None) -> dict[str, Any]: ...
    async def click(self, control_id: str) -> dict[str, Any]: ...
    async def inspect_widget(self) -> dict[str, Any]: ...
    async def open_field(self, field_id: str) -> dict[str, Any]: ...
    async def click_widget_option(self, option_id: str) -> dict[str, Any]: ...
    async def click_widget_control(self, widget_control_id: str) -> dict[str, Any]: ...
    async def select_cascade(self, field_id: str, path: list[Any]) -> dict[str, Any]: ...
    async def set_date(self, field_id: str, value: Any) -> dict[str, Any]: ...


ConfirmHandler = Callable[[str], bool | Awaitable[bool]]
PauseHandler = Callable[[str], Any | Awaitable[Any]]


async def _default_confirm(message: str) -> bool:
    answer = await asyncio.to_thread(input, f"\n{message}\n允许？[y/N] ")
    return answer.strip().lower() in {"y", "yes", "是", "允许"}


async def _default_pause(message: str) -> None:
    await asyncio.to_thread(input, f"\n{message}\n完成后按 Enter 继续… ")


class FormPilotTools:
    def __init__(
        self,
        browser: BrowserLike,
        profile: ProfileStore,
        *,
        policy: ApprovalPolicy | None = None,
        confirm: ConfirmHandler | None = None,
        pause: PauseHandler | None = None,
    ) -> None:
        self.browser = browser
        self.profile = profile
        self.policy = policy or ApprovalPolicy()
        self.confirm_handler = confirm or _default_confirm
        self.pause_handler = pause or _default_pause

    async def _call_maybe_async(self, handler: Callable[..., Any], *args: Any) -> Any:
        result = handler(*args)
        if hasattr(result, "__await__"):
            return await result
        return result

    def _sanitize_field(self, field: dict[str, Any]) -> dict[str, Any]:
        """Return page semantics without sending user-entered values to the model."""
        sanitized = dict(field)
        current = sanitized.pop("current_value", "")
        sanitized["has_value"] = bool(current)
        if sanitized.get("type") in {"checkbox", "radio"}:
            sanitized["checked"] = bool(current)
        return sanitized

    def _public_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "title": snapshot["title"],
            "url": snapshot["url"],
            "fields": [self._sanitize_field(field) for field in snapshot["fields"][:120]],
            "controls": snapshot["controls"][:50],
            "feedback": snapshot.get("feedback", [])[:30],
        }

    async def inspect_page(self) -> dict[str, Any]:
        snapshot = await self.browser.inspect()
        return self._public_snapshot(snapshot)

    async def get_profile_catalog(self) -> dict[str, Any]:
        return {
            "ok": True,
            "privacy": "profile values are omitted; use paths with a local fill/select/date tool",
            "fields": self.profile.catalog(),
        }

    async def inspect_widget(self) -> dict[str, Any]:
        widget = await self.browser.inspect_widget()
        return {
            "ok": True,
            "widgets": widget["widgets"][:30],
            "options": widget["options"][:300],
            "controls": widget["controls"][:200],
        }

    async def open_field(self, field_id: str) -> dict[str, Any]:
        return await self.browser.open_field(field_id)

    async def click_widget_option(self, option_id: str) -> dict[str, Any]:
        return await self.browser.click_widget_option(option_id)

    async def click_widget_control(self, widget_control_id: str) -> dict[str, Any]:
        return await self.browser.click_widget_control(widget_control_id)

    async def fill_from_profile(self, field_id: str, profile_path: str, approval_id: str | None) -> dict[str, Any]:
        snapshot = await self.browser.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        if field is None:
            return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        if self.policy.secret_field(field):
            return {"ok": False, "blocked": True, "error": "密码、验证码、文件或签名字段必须由用户处理"}

        try:
            value = self.profile.get(profile_path)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

        target = f"fill:{snapshot['url']}:{field_id}:{profile_path}"
        if profile_path in HIGH_RISK_PATHS and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"向 {snapshot['url']} 的“{field['label']}”填写本地资料 {profile_path}",
            }

        result = await self.browser.fill(field_id, value)
        verification = result.get("verification")
        if isinstance(verification, dict):
            verification.pop("actual", None)
        result["profile_path"] = profile_path
        result["value_privacy"] = "真实值仅在本地浏览器工具中使用，未返回模型"
        return result

    async def verify_field(self, field_id: str) -> dict[str, Any]:
        result = await self.browser.verify(field_id)
        actual = result.pop("actual", "")
        result["has_value"] = bool(actual)
        return result

    async def select_cascade_from_profile(
        self,
        field_id: str,
        profile_paths: list[str],
        approval_id: str | None,
    ) -> dict[str, Any]:
        snapshot = await self.browser.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        if field is None:
            return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        try:
            values = [self.profile.get(path) for path in profile_paths]
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        target = f"cascade:{snapshot['url']}:{field_id}:{','.join(profile_paths)}"
        if any(path in HIGH_RISK_PATHS for path in profile_paths) and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"向 {snapshot['url']} 的“{field['label']}”逐级选择本地资料 {profile_paths}",
            }
        result = await self.browser.select_cascade(field_id, values)
        result["profile_paths"] = profile_paths
        result.pop("actual", None)
        result["value_privacy"] = "级联值仅在本地浏览器工具中使用，未返回模型"
        return result

    async def set_date_from_profile(self, field_id: str, profile_path: str, approval_id: str | None) -> dict[str, Any]:
        snapshot = await self.browser.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        if field is None:
            return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        try:
            value = self.profile.get(profile_path)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        target = f"date:{snapshot['url']}:{field_id}:{profile_path}"
        if profile_path in HIGH_RISK_PATHS and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"向 {snapshot['url']} 的“{field['label']}”选择本地日期资料 {profile_path}",
            }
        result = await self.browser.set_date(field_id, value)
        result["profile_path"] = profile_path
        result["value_privacy"] = "日期原值仅在本地浏览器工具中使用，未返回模型"
        return result

    async def wait_and_rescan(self, milliseconds: int) -> dict[str, Any]:
        snapshot = await self.browser.wait_and_rescan(milliseconds)
        return self._public_snapshot(snapshot)

    async def request_user_confirmation(self, target: str, summary: str) -> dict[str, Any]:
        allowed = bool(await self._call_maybe_async(self.confirm_handler, summary))
        if not allowed:
            return {"ok": False, "approved": False, "message": "用户拒绝了该操作"}
        approval_id = self.policy.issue(target)
        return {"ok": True, "approved": True, "approval_id": approval_id, "target": target, "single_use": True}

    async def click_control(self, control_id: str, approval_id: str | None) -> dict[str, Any]:
        snapshot = await self.browser.inspect()
        control = next((item for item in snapshot["controls"] if item["control_id"] == control_id), None)
        if control is None:
            return {"ok": False, "error": "控件已消失，请重新 inspect_page"}
        target = f"click:{snapshot['url']}:{control_id}"
        if self.policy.click_requires_confirmation(control) and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"在 {snapshot['url']} 点击“{control['label']}”",
            }
        return await self.browser.click(control_id)

    async def pause_for_user(self, message: str) -> dict[str, Any]:
        await self._call_maybe_async(self.pause_handler, message)
        snapshot = await self.browser.inspect()
        return {
            "ok": True,
            "message": "用户已接管并返回",
            "url": snapshot["url"],
            "feedback": snapshot.get("feedback", [])[:30],
        }

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        nullable_approval = {"type": ["string", "null"], "description": "一次性审批 ID；首次调用传 null"}

        registry.register(Tool("inspect_page", "读取当前页面的可见字段、选项、按钮和校验提示。每次页面变化后调用。", empty, self.inspect_page))
        registry.register(Tool("get_profile_catalog", "列出本地资料的语义路径。高风险原值不会暴露给模型。", empty, self.get_profile_catalog))
        registry.register(Tool("inspect_widget", "读取当前已打开的下拉、级联、树形或日历弹层，返回可见选项和弹层按钮。", empty, self.inspect_widget))
        registry.register(Tool(
            "fill_from_profile",
            "把一个本地资料路径的真实值填写到网页字段并回读验证。高风险资料首次会要求确认。",
            {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string", "description": "inspect_page 返回的字段 ID"},
                    "profile_path": {"type": "string", "description": "get_profile_catalog 返回的资料路径"},
                    "approval_id": nullable_approval,
                },
                "required": ["field_id", "profile_path", "approval_id"],
                "additionalProperties": False,
            },
            self.fill_from_profile,
        ))
        registry.register(Tool(
            "open_field",
            "点击字段以打开自定义下拉、级联选择器或日期面板，然后返回弹层结构。",
            {
                "type": "object",
                "properties": {"field_id": {"type": "string"}},
                "required": ["field_id"],
                "additionalProperties": False,
            },
            self.open_field,
        ))
        registry.register(Tool(
            "click_widget_option",
            "点击 inspect_widget 返回的单个可见弹层选项。适用于自定义 select、树节点和日期格。",
            {
                "type": "object",
                "properties": {"option_id": {"type": "string"}},
                "required": ["option_id"],
                "additionalProperties": False,
            },
            self.click_widget_option,
        ))
        registry.register(Tool(
            "click_widget_control",
            "点击 inspect_widget 返回的弹层控制按钮，例如上一年、下一年、上一月或下一月。",
            {
                "type": "object",
                "properties": {"widget_control_id": {"type": "string"}},
                "required": ["widget_control_id"],
                "additionalProperties": False,
            },
            self.click_widget_control,
        ))
        registry.register(Tool(
            "select_cascade_from_profile",
            "打开一个自定义级联选择器，并按多个本地资料路径逐层选择，例如省、市、区。原值不发送给模型。",
            {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "profile_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
                    "approval_id": nullable_approval,
                },
                "required": ["field_id", "profile_paths", "approval_id"],
                "additionalProperties": False,
            },
            self.select_cascade_from_profile,
        ))
        registry.register(Tool(
            "set_date_from_profile",
            "设置原生或自定义日期字段。自定义日历会读取当前年月并有限次点击上一年/下一年/上一月/下一月后选择日期。",
            {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "profile_path": {"type": "string"},
                    "approval_id": nullable_approval,
                },
                "required": ["field_id", "profile_path", "approval_id"],
                "additionalProperties": False,
            },
            self.set_date_from_profile,
        ))
        registry.register(Tool(
            "verify_field",
            "读取单个字段的当前值，用于确认页面是否接受了之前的填写。",
            {
                "type": "object",
                "properties": {"field_id": {"type": "string"}},
                "required": ["field_id"],
                "additionalProperties": False,
            },
            self.verify_field,
        ))
        registry.register(Tool(
            "wait_and_rescan",
            "等待动态选项或页面局部刷新，然后重新扫描。",
            {
                "type": "object",
                "properties": {"milliseconds": {"type": "integer", "minimum": 0, "maximum": 5000}},
                "required": ["milliseconds"],
                "additionalProperties": False,
            },
            self.wait_and_rescan,
        ))
        registry.register(Tool(
            "request_user_confirmation",
            "向用户请求一次性授权。仅在其他工具返回 confirmation_required 和 target 后调用。",
            {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "工具返回的原样 target"},
                    "summary": {"type": "string", "description": "向用户清楚解释域名、数据或点击动作"},
                },
                "required": ["target", "summary"],
                "additionalProperties": False,
            },
            self.request_user_confirmation,
        ))
        registry.register(Tool(
            "click_control",
            "点击 inspect_page 返回的按钮。除明确的上下页导航外通常需要一次性授权。",
            {
                "type": "object",
                "properties": {
                    "control_id": {"type": "string"},
                    "approval_id": nullable_approval,
                },
                "required": ["control_id", "approval_id"],
                "additionalProperties": False,
            },
            self.click_control,
        ))
        registry.register(Tool(
            "pause_for_user",
            "暂停 Agent，让用户在可见浏览器中手动登录、输入密码/验证码或处理歧义。不要让用户把秘密发给模型。",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            self.pause_for_user,
        ))
        return registry
