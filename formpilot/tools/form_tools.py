from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from ..credentials import (
    is_human_secret_field,
    is_image_captcha_field,
    is_password_field,
    is_project_select_field,
    is_sms_secret_field,
    is_username_field,
    load_login_credentials,
    match_project_option,
    match_select_option,
)
from ..policy import ApprovalPolicy, LOGIN_CONTROL_PATTERN
from ..profile import PROFILE_LABELS, ProfileStore
from ..task import TaskBrief
from .base import Tool, ToolRegistry


NEXT_STEP_PATTERN = re.compile(r"下一步|继续|下一页|保存并下一步", re.I)
SKIP_REQUIRED_TYPES = {"hidden", "submit", "button", "reset", "image", "file", "password"}


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
    async def search_visible_text(self, query: str, *, limit: int = 20) -> dict[str, Any]: ...
    async def capture_captcha_image(self, field_id: str) -> dict[str, Any]: ...


ConfirmHandler = Callable[[str], bool | Awaitable[bool]]
PauseHandler = Callable[[str], Any | Awaitable[Any]]


async def _default_confirm(message: str) -> bool:
    answer = await asyncio.to_thread(input, f"\n{message}\n允许？[y/N] ")
    return answer.strip().lower() in {"y", "yes", "是", "允许"}


async def _default_pause(message: str) -> str:
    reply = await asyncio.to_thread(
        input,
        f"\n{message}\n完成后按 Enter 继续；也可输入自然语言指引后回车：\n> ",
    )
    return reply.strip()


class FormPilotTools:
    def __init__(
        self,
        browser: BrowserLike,
        profile: ProfileStore,
        *,
        task: TaskBrief | None = None,
        policy: ApprovalPolicy | None = None,
        confirm: ConfirmHandler | None = None,
        pause: PauseHandler | None = None,
        profile_path: str | Path | None = None,
        template_path: str | Path | None = None,
    ) -> None:
        self.browser = browser
        self.profile = profile
        self.task = task
        self.policy = policy or ApprovalPolicy()
        self.confirm_handler = confirm or _default_confirm
        self.pause_handler = pause or _default_pause
        if profile_path is not None:
            self.profile.path = Path(profile_path)
        if template_path is not None:
            self.profile.template_path = Path(template_path)
        elif self.profile.path is not None and self.profile.template_path is None:
            self.profile.template_path = self.profile.path.with_name("profile.json.template")

    async def _call_maybe_async(self, handler: Callable[..., Any], *args: Any) -> Any:
        result = handler(*args)
        if hasattr(result, "__await__"):
            return await result
        return result

    @staticmethod
    def _placeholder_like(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return True
        return bool(re.match(r"^(请选择|点击选择|选择|请填写)", text)) or text in {"-", "--", "—"}

    def _sanitize_field(self, field: dict[str, Any]) -> dict[str, Any]:
        """Return page semantics without sending user-entered values to the model."""
        sanitized = dict(field)
        current = sanitized.pop("current_value", "")
        if "has_value" in sanitized:
            sanitized["has_value"] = bool(sanitized.get("has_value"))
        else:
            sanitized["has_value"] = bool(current)
        if self._placeholder_like(current):
            sanitized["has_value"] = False
        if sanitized.get("type") == "select" and sanitized.get("options"):
            selected = next((item for item in sanitized["options"] if item.get("selected")), None)
            if selected is not None:
                text = str(selected.get("text") or "")
                value = str(selected.get("value") or "")
                if value in {"", "-1"} or text.startswith("请选择") or text.startswith("-"):
                    sanitized["has_value"] = False
        if sanitized.get("type") in {"checkbox", "radio"}:
            sanitized["checked"] = bool(current)
        label = str(sanitized.get("label") or "")
        needs_cascade = bool(sanitized.get("needs_cascade"))
        if re.search(r"出生地|籍贯|户口所在地|档案所在地|生源地|所在地区", label) and not re.search(
            r"详细|单位(?!地)|邮编|邮政|编码", label
        ):
            needs_cascade = True
        if sanitized.get("read_only") and re.search(r"出生地|籍贯|户口|所在地|省市|地区|归属地", label):
            needs_cascade = True
        if re.search(r"所在学校|毕业院校|学校名称|所在专业|所学专业|专业名称|本科院校", label):
            needs_cascade = True
        if (sanitized.get("disabled") or sanitized.get("read_only")) and re.search(
            r"bkbydw|bkbyzy|bydw|byzy|Show$", str(sanitized.get("id") or "") + str(sanitized.get("name") or ""), re.I
        ):
            needs_cascade = True
        needs_month = bool(sanitized.get("needs_month"))
        if re.search(r"入学年月|毕业年月|预计毕业", label) or (
            re.search(r"年月", label) and not re.search(r"日", label.replace("年月", ""))
        ):
            needs_month = True
        if needs_cascade:
            sanitized["needs_cascade"] = True
            sanitized["fill_hint"] = (
                "优先 select_cascade_from_profile（学校/专业可只传单路径如 education.school）；"
                "失败则 open_field（会点同格「选择」）→ inspect_widget → "
                "click_visible_text → confirm_overlay"
            )
            # Disabled region/catalog display boxes still need to be filled via cascade.
            if not sanitized.get("has_value"):
                sanitized["disabled"] = False
        if needs_month:
            sanitized["needs_month"] = True
            sanitized["fill_hint"] = (
                "优先 set_date_from_profile（入学用 education.enrollment_date，"
                "毕业用 education.graduation_date；支持 yyyy-MM）"
            )
            if not sanitized.get("has_value"):
                sanitized["disabled"] = False
        return sanitized

    @staticmethod
    def _incomplete_required_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
        incomplete: list[dict[str, Any]] = []
        for field in fields:
            if not isinstance(field, dict):
                continue
            # Readonly cascade/month pickers must still be required-checked.
            # Only skip truly inert disabled controls (not picker-style fields).
            if field.get("disabled") and not (
                field.get("read_only") or field.get("needs_cascade") or field.get("needs_month")
            ):
                continue
            if str(field.get("type") or "").lower() in SKIP_REQUIRED_TYPES:
                continue
            if not field.get("required"):
                continue
            if field.get("has_value"):
                continue
            item = {
                "field_id": field.get("field_id"),
                "label": field.get("label") or field.get("name") or field.get("id") or "未命名字段",
                "type": field.get("type"),
                "id": field.get("id"),
                "name": field.get("name"),
            }
            if field.get("read_only") or field.get("needs_cascade"):
                item["needs_cascade"] = True
                item["fill_hint"] = field.get("fill_hint") or (
                    "select_cascade_from_profile，或 open_field → inspect_widget → "
                    "click_visible_text → confirm_overlay"
                )
            if field.get("needs_month"):
                item["needs_month"] = True
                item["fill_hint"] = field.get("fill_hint") or (
                    "set_date_from_profile（education.enrollment_date / education.graduation_date）"
                )
            incomplete.append(item)
        return incomplete

    def _public_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        fields = [self._sanitize_field(field) for field in snapshot["fields"][:120]]
        incomplete = self._incomplete_required_fields(fields)
        return {
            "ok": True,
            "title": snapshot["title"],
            "url": snapshot["url"],
            "fields": fields,
            "controls": snapshot["controls"][:80],
            "feedback": snapshot.get("feedback", [])[:30],
            "incomplete_required": incomplete,
            "incomplete_required_count": len(incomplete),
        }

    async def inspect_page(self) -> dict[str, Any]:
        if hasattr(self.browser, "maybe_recover_session_error"):
            recovery = await self.browser.maybe_recover_session_error()
            if recovery.get("hit"):
                snapshot = await self.browser.inspect()
                public = self._public_snapshot(snapshot)
                public["session_recovery"] = recovery
                if recovery.get("recovered"):
                    public["message"] = recovery.get("message") or "已清理异常会话并重新打开登录页"
                return public
        snapshot = await self.browser.inspect()
        return self._public_snapshot(snapshot)

    async def get_profile_catalog(self) -> dict[str, Any]:
        return {
            "ok": True,
            "privacy": (
                "用户已授权模型阅读本地个人资料原值，用于表单推理填写；"
                "密码/短信验证码仍不可用资料填入。请结合页面空字段尽量填完。"
            ),
            "fields": self.profile.catalog(include_values=True),
            "note": (
                "若页面字段在资料中无同名路径，可根据资料做合理推理后用 fill_text 填写"
                "（例如由证件号推导出生日期/性别，由学校或地址匹配省市选项）。"
            ),
        }

    async def read_profile(self) -> dict[str, Any]:
        """Return the full local profile for model reasoning."""
        return {
            "ok": True,
            "privacy": "本地个人资料全文，仅用于本机表单填写推理。",
            "profile": self.profile.public_profile(),
        }

    async def read_task_brief(self) -> dict[str, Any]:
        if self.task is None:
            return {"ok": False, "error": "未加载任务文档；可用 --task path/to/task.md 提供"}
        return self.task.public_view()

    async def search_visible_text(self, query: str) -> dict[str, Any]:
        return await self.browser.search_visible_text(query)

    async def fill_from_task_fact(self, field_id: str, fact_key: str) -> dict[str, Any]:
        if self.task is None:
            return {"ok": False, "error": "未加载任务文档"}
        snapshot = await self.browser.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        if field is None:
            return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        if self.policy.secret_field(field):
            return {"ok": False, "blocked": True, "error": "密码、验证码、文件或签名字段必须由用户处理"}
        try:
            value = self.task.get_fact(fact_key)
        except KeyError as exc:
            return {"ok": False, "error": str(exc), "available_facts": [item["key"] for item in self.task.fact_catalog()]}
        result = await self.browser.fill(field_id, value)
        verification = result.get("verification")
        if isinstance(verification, dict):
            verification.pop("actual", None)
        return {
            "ok": bool(result.get("ok")),
            "field_id": field_id,
            "fact_key": fact_key,
            "verification": verification,
            "error": result.get("error"),
            "available_options": result.get("available_options"),
        }

    async def inspect_widget(self) -> dict[str, Any]:
        widget = await self.browser.inspect_widget()
        return {
            "ok": True,
            "widgets": widget["widgets"][:30],
            "options": widget["options"][:300],
            "controls": widget["controls"][:200],
            "iframe": widget.get("iframe"),
            "hint": (
                "若 options 含 source=iframe，地区树在弹层 iframe 内。"
                "优先 select_cascade_from_profile；或 click_visible_text（会先用关键字搜索再点叶子节点），"
                "选完后 confirm_overlay。未选完前不要 dismiss_page_overlays。"
            ),
        }

    async def open_field(self, field_id: str) -> dict[str, Any]:
        return await self.browser.open_field(field_id)

    async def click_widget_option(self, option_id: str) -> dict[str, Any]:
        return await self.browser.click_widget_option(option_id)

    async def click_widget_control(self, widget_control_id: str) -> dict[str, Any]:
        return await self.browser.click_widget_control(widget_control_id)

    async def click_visible_text(self, text: str, where: str = "auto") -> dict[str, Any]:
        if hasattr(self.browser, "click_visible_text"):
            return await self.browser.click_visible_text(text, where=where)
        return {"ok": False, "error": "当前浏览器未实现 click_visible_text"}

    async def confirm_overlay(self) -> dict[str, Any]:
        if hasattr(self.browser, "confirm_overlay"):
            return await self.browser.confirm_overlay()
        return {"ok": False, "error": "当前浏览器未实现 confirm_overlay"}

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

        result = await self.browser.fill(field_id, value)
        verification = result.get("verification")
        if isinstance(verification, dict):
            verification.pop("actual", None)
        result["profile_path"] = profile_path
        result["value_privacy"] = "已使用本地资料填写；验证信息中不含原值回显"
        return result

    async def fill_text(self, field_id: str, value: str, *, reason: str = "") -> dict[str, Any]:
        """Fill a non-secret field with a model-inferred or transformed value."""
        text = str(value or "").strip()
        if not text:
            return {"ok": False, "error": "value 不能为空"}
        if len(text) > 500:
            return {"ok": False, "error": "value 过长"}
        snapshot = await self.browser.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        if field is None:
            return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        if self.policy.secret_field(field):
            return {"ok": False, "blocked": True, "error": "密码、验证码、文件或签名字段必须由用户处理"}
        result = await self.browser.fill(field_id, text)
        verification = result.get("verification")
        if isinstance(verification, dict):
            verification.pop("actual", None)
        return {
            "ok": bool(result.get("ok")),
            "field_id": field_id,
            "filled_via": "fill_text",
            "reason": (reason or "").strip()[:200],
            "verification": verification,
            "error": result.get("error"),
            "available_options": result.get("available_options"),
            "value_privacy": "推理填写值已写入页面；工具结果不回传完整原值",
        }

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
        result = await self.browser.select_cascade(field_id, values)
        result["profile_paths"] = profile_paths
        result.pop("actual", None)
        result["value_privacy"] = "已使用本地资料逐级填写"
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
        result = await self.browser.set_date(field_id, value)
        result["profile_path"] = profile_path
        result.pop("actual", None)
        result["value_privacy"] = "已使用本地资料填写日期"
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
        label = str(control.get("label") or "")
        if NEXT_STEP_PATTERN.search(label):
            public = self._public_snapshot(snapshot)
            incomplete = public.get("incomplete_required") or []
            if incomplete:
                return {
                    "ok": False,
                    "blocked": True,
                    "error": "还有必填项未填写，禁止点击下一步",
                    "incomplete_required": incomplete,
                    "hint": (
                        "请先填完 incomplete_required；地区/学校/专业可试 select_cascade_from_profile，"
                        "年月用 set_date_from_profile；失败则 open_field → inspect_widget → "
                        "click_visible_text → confirm_overlay；资料不足则 request_missing_profile_fields。"
                    ),
                }
        target = f"click:{snapshot['url']}:{control_id}"
        if self.policy.click_requires_confirmation(control) and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"在 {snapshot['url']} 点击“{control['label']}”",
            }
        return await self.browser.click(control_id)

    async def dismiss_page_overlays(self) -> dict[str, Any]:
        """Close common modal/shade layers that block clicks (e.g. layui)."""
        if not hasattr(self.browser, "page") or self.browser.page is None:
            # Fake browsers in tests may not have page.
            if hasattr(self.browser, "dismiss_page_overlays"):
                return await self.browser.dismiss_page_overlays()  # type: ignore[misc]
            return {"ok": True, "dismissed": 0}
        result = await self.browser.page.evaluate(
            """() => {
              let dismissed = 0;
              const shades = Array.from(document.querySelectorAll('.layui-layer-shade, .layui-layer'));
              for (const node of shades) {
                node.remove();
                dismissed += 1;
              }
              return {dismissed};
            }"""
        )
        await self.browser.wait_and_rescan(400)
        public = self._public_snapshot(await self.browser.inspect())
        public["dismissed"] = int((result or {}).get("dismissed") or 0)
        public["message"] = f"已尝试关闭弹层 {public['dismissed']} 个"
        return public

    async def request_missing_profile_fields(self, fields: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask the human in the terminal for missing profile values, then persist them."""
        if not fields:
            return {"ok": False, "error": "fields 不能为空"}

        print("\n========== 需要补充个人资料 ==========", flush=True)
        print("以下必填/空缺项无法仅从现有资料可靠填写。请在终端逐项输入；直接回车表示跳过该项。", flush=True)
        updates: dict[str, str] = {}
        collected: list[dict[str, str]] = []
        for index, item in enumerate(fields, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("field_label") or f"字段{index}").strip()
            path = str(item.get("profile_path") or item.get("path") or "").strip()
            hint = str(item.get("hint") or item.get("reason") or "").strip()
            if not path:
                # Derive a stable custom path under extras if model forgot.
                slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", label).strip("_") or f"field_{index}"
                path = f"extras.{slug}"
            prompt = f"[{index}/{len(fields)}] {label} ({path})"
            if hint:
                prompt += f"\n  说明：{hint}"
            prompt += "\n  请输入值后回车（留空跳过）："
            print(prompt, flush=True)
            try:
                value = input().strip()
            except EOFError:
                value = ""
            if not value:
                collected.append({"label": label, "profile_path": path, "status": "skipped"})
                continue
            updates[path] = value
            collected.append({"label": label, "profile_path": path, "status": "saved"})
            if path not in PROFILE_LABELS:
                PROFILE_LABELS[path] = label

        changed = self.profile.update_many(updates)
        saved_profile = None
        saved_template = None
        if changed:
            try:
                saved_profile = str(self.profile.save())
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"写入 profile.json 失败：{type(exc).__name__}",
                    "collected": collected,
                }
            try:
                saved_template = str(self.profile.sync_template())
            except Exception as exc:
                return {
                    "ok": False,
                    "error": f"写入 profile.json.template 失败：{type(exc).__name__}",
                    "profile_path": saved_profile,
                    "collected": collected,
                    "updated_paths": changed,
                }

        print("====================================\n", flush=True)
        return {
            "ok": True,
            "updated_paths": changed,
            "collected": collected,
            "profile_path": saved_profile,
            "template_path": saved_template,
            "profile": self.profile.public_profile(),
            "message": (
                f"已更新 {len(changed)} 项资料并写入本地文件。"
                if changed
                else "未收到新输入；资料文件未改动。"
            ),
        }

    async def attempt_auto_login(self) -> dict[str, Any]:
        """Fill credentials + captcha, auto-click login, retry captcha on failure."""
        if hasattr(self.browser, "maybe_recover_session_error"):
            recovery = await self.browser.maybe_recover_session_error()
            if recovery.get("hit") and not recovery.get("ok", True):
                return {
                    "ok": False,
                    "error": recovery.get("error") or "会话异常页恢复失败",
                    "session_recovery": recovery,
                }
        snapshot = await self.browser.inspect()
        creds = load_login_credentials(page_url=snapshot.get("url"))
        if creds is None or not creds.configured:
            return {
                "ok": False,
                "configured": False,
                "error": "未配置临时登录凭据。可在 .env 设置 FORMPILOT_LOGIN_USERNAME/PASSWORD，或使用 credentials.json",
            }

        try:
            max_attempts = max(1, min(int(os.getenv("FORMPILOT_CAPTCHA_RETRIES") or "5"), 10))
        except ValueError:
            max_attempts = 5

        filled: list[str] = []
        errors: list[str] = []
        selected_project = None
        captcha_ok = False
        captcha_length = 0
        captcha_backend = ""
        clicked_login = False
        attempts = 0
        guidance = self.task.text if self.task is not None else ""

        def _still_login(url: str) -> bool:
            lowered = str(url or "").lower()
            return "logon" in lowered or "login" in lowered

        def _captcha_error_visible(snap: dict[str, Any]) -> bool:
            blobs: list[str] = []
            for item in snap.get("feedback") or []:
                blobs.append(str(item))
            for field in snap.get("fields") or []:
                blobs.append(str(field.get("label") or ""))
            text = " ".join(blobs)
            # Inspect feedback may be sparse; also check via search when available below.
            return "验证码错误" in text or "验证码有误" in text

        async def _page_says_captcha_error() -> bool:
            try:
                found = await self.browser.search_visible_text("验证码错误", limit=5)
                return bool(found.get("matches"))
            except Exception:
                return False

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            snapshot = await self.browser.inspect()
            fields = snapshot.get("fields") or []
            controls = snapshot.get("controls") or []
            username_fields = [field for field in fields if is_username_field(field)]
            password_fields = [field for field in fields if is_password_field(field)]
            captcha_fields = [field for field in fields if is_image_captcha_field(field)]
            project_fields = [field for field in fields if is_project_select_field(field)]
            sms_fields = [field for field in fields if is_sms_secret_field(field)]

            if sms_fields:
                errors.append("sms_required")
                break

            # Re-fill each attempt: failed login often resets the form.
            if len(project_fields) == 1:
                options = project_fields[0].get("options") or []
                option = match_project_option(options, guidance)
                if option is None and self.profile is not None:
                    try:
                        option_text = str(self.profile.get("application.project"))
                    except KeyError:
                        option_text = ""
                    if option_text:
                        option = match_select_option(options, option_text)
                if option is not None:
                    result = await self.browser.fill(
                        project_fields[0]["field_id"], option.get("text") or option.get("value")
                    )
                    if result.get("ok"):
                        if "project" not in filled:
                            filled.append("project")
                        selected_project = option.get("text")
                    else:
                        errors.append(f"project:{result.get('error') or 'fill_failed'}")
                else:
                    errors.append("project_option_not_matched")

            if len(username_fields) == 1:
                result = await self.browser.fill(username_fields[0]["field_id"], creds.username)
                if result.get("ok"):
                    if "username" not in filled:
                        filled.append("username")
                else:
                    errors.append(f"username:{result.get('error') or 'fill_failed'}")
            elif not username_fields:
                errors.append("username_field_not_found")
                break
            else:
                errors.append("username_field_ambiguous")
                break

            if len(password_fields) == 1:
                result = await self.browser.fill(password_fields[0]["field_id"], creds.password)
                if result.get("ok"):
                    if "password" not in filled:
                        filled.append("password")
                else:
                    errors.append(f"password:{result.get('error') or 'fill_failed'}")
            elif not password_fields:
                errors.append("password_field_not_found")
                break
            else:
                errors.append("password_field_ambiguous")
                break

            captcha_ok = False
            if len(captcha_fields) != 1:
                if captcha_fields:
                    errors.append("captcha_field_ambiguous")
                # No captcha: try login once.
            else:
                try:
                    from ..captcha import recognize_captcha_image

                    image = await self.browser.capture_captcha_image(captcha_fields[0]["field_id"])
                    if not image.get("ok"):
                        errors.append(f"captcha_image:{image.get('error') or 'missing'}")
                        print(f"[captcha] 第{attempt}/{max_attempts}次取图失败，重试…", flush=True)
                        await asyncio.sleep(0.4)
                        continue
                    recognized = await recognize_captcha_image(image["png"])
                    code = str(recognized.get("text") or "")
                    captcha_backend = str(recognized.get("backend") or "")
                    captcha_length = len(code)
                    debug_path = str(recognized.get("debug_path") or "")
                    if debug_path:
                        print(f"\n[captcha] 截图已保存：{debug_path}", flush=True)
                    print(
                        f"[captcha] 第{attempt}/{max_attempts}次 "
                        f"method={image.get('method') or 'unknown'} "
                        f"{image.get('width')}x{image.get('height')} "
                        f"backend={captcha_backend or '-'} len={captcha_length}",
                        flush=True,
                    )
                    if not code:
                        err = str(recognized.get("error") or "unknown")
                        errors.append(f"captcha_ocr_empty:{err}")
                        print(f"[captcha] 识别失败：{err}，刷新重试…", flush=True)
                        await asyncio.sleep(0.4)
                        continue
                    result = await self.browser.fill(captcha_fields[0]["field_id"], code)
                    if not result.get("ok"):
                        errors.append(f"captcha:{result.get('error') or 'fill_failed'}")
                        continue
                    if "captcha" not in filled:
                        filled.append("captcha")
                    captcha_ok = True
                    print(f"[captcha] 识别结果：{code}（已填入，自动点击登录）", flush=True)
                except Exception as exc:
                    errors.append(f"captcha_ocr:{type(exc).__name__}")
                    print(f"[captcha] 异常 {type(exc).__name__}，重试…", flush=True)
                    continue

            login_controls = [
                control
                for control in controls
                if LOGIN_CONTROL_PATTERN.search(str(control.get("label", ""))) and not control.get("disabled")
            ]
            if len(login_controls) != 1:
                errors.append("login_button_not_found" if not login_controls else "login_button_ambiguous")
                break
            try:
                click_result = await self.browser.click(login_controls[0]["control_id"])
                clicked_login = bool(click_result.get("ok"))
                if clicked_login and "login_click" not in filled:
                    filled.append("login_click")
                await self.browser.wait_and_rescan(1500)
            except Exception as exc:
                errors.append(f"login_click:{type(exc).__name__}")
                break

            public_mid = self._public_snapshot(await self.browser.inspect())
            if not _still_login(str(public_mid.get("url", ""))):
                print(f"[captcha] 登录成功（第{attempt}次尝试）", flush=True)
                break
            if await _page_says_captcha_error() or _captcha_error_visible(await self.browser.inspect()):
                print(f"[captcha] 验证码未通过，自动重试（{attempt}/{max_attempts}）…", flush=True)
                await asyncio.sleep(0.5)
                continue
            # Still on login without explicit captcha error: retry captcha anyway.
            print(f"[captcha] 仍在登录页，自动重试（{attempt}/{max_attempts}）…", flush=True)
            await asyncio.sleep(0.5)

        public = self._public_snapshot(await self.browser.inspect())
        needs_human = []
        for field in public.get("fields") or []:
            if is_sms_secret_field(field) and not field.get("has_value"):
                needs_human.append(str(field.get("label") or "短信验证码"))
            elif is_image_captcha_field(field) and not field.get("has_value"):
                needs_human.append(str(field.get("label") or "验证码"))
            elif is_human_secret_field(field) and not field.get("has_value"):
                needs_human.append(str(field.get("label") or "人工项"))

        still_login = _still_login(str(public.get("url", "")))
        ok = "username" in filled and "password" in filled and not still_login
        if still_login:
            ok = False
            needs_human.append("图形验证码自动重试已用尽，请手动填写并登录")
            print(
                f"\n[captcha] 自动登录未成功（尝试 {attempts} 次）。请手动填写验证码并点登录，然后按 Enter。\n",
                flush=True,
            )
            await self._call_maybe_async(
                self.pause_handler,
                (
                    f"图形验证码已自动识别并尝试登录 {attempts} 次仍未通过。\n"
                    "请手动核对着页面填写验证码并点击「登录」。\n"
                    "登录成功后按 Enter 继续。"
                ),
            )

        public.update(
            {
                "ok": ok and not needs_human,
                "configured": True,
                "source": creds.source,
                "filled": filled,
                "selected_project": selected_project,
                "captcha_ocr_length": captcha_length,
                "captcha_backend": captcha_backend,
                "captcha_attempts": attempts,
                "clicked_login": clicked_login,
                "needs_human": needs_human,
                "errors": errors[-12:],
                "message": (
                    "已自动处理招生项目/账号密码"
                    + ("/图形验证码" if captcha_ok or "captcha" in filled else "")
                    + ("并尝试点击登录" if clicked_login else "")
                    + f"（尝试 {attempts} 次；敏感原值未回传模型）。"
                    + (
                        "请处理短信验证码等人工项。"
                        if needs_human
                        else ("登录成功，可继续填表。" if not still_login else "仍在登录页。")
                    )
                ),
            }
        )
        return public

    async def pause_for_user(self, message: str) -> dict[str, Any]:
        reply = await self._call_maybe_async(self.pause_handler, message)
        snapshot = await self.browser.inspect()
        public = self._public_snapshot(snapshot)
        public["message"] = "用户已接管并返回"
        guidance = ""
        if isinstance(reply, str):
            guidance = reply.strip()
        elif isinstance(reply, dict):
            guidance = str(reply.get("guidance") or reply.get("user_guidance") or "").strip()
        if guidance:
            public["user_guidance"] = guidance
        return public

    def registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        nullable_approval = {"type": ["string", "null"], "description": "一次性审批 ID；首次调用传 null"}

        registry.register(Tool("inspect_page", "读取当前页面的可见字段、选项、按钮、链接和校验提示。每次页面变化后调用。", empty, self.inspect_page))
        registry.register(Tool(
            "get_profile_catalog",
            "列出本地个人资料（含具体值与路径）。进入填表前调用；可据此直接填写或推理后 fill_text。",
            empty,
            self.get_profile_catalog,
        ))
        registry.register(Tool(
            "read_profile",
            "读取完整本地个人资料 JSON，便于综合推理页面上没有直接对应项的字段。",
            empty,
            self.read_profile,
        ))
        registry.register(Tool("read_task_brief", "阅读用户用自然语言写的任务说明：要报什么、怎么走、填写注意点。开始时优先调用。", empty, self.read_task_brief))
        registry.register(Tool(
            "search_visible_text",
            "在当前页面可见文本中搜索关键词，用于从页面说明/菜单/选项中寻找答案或入口。",
            {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "要搜索的关键词或短语"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            self.search_visible_text,
        ))
        registry.register(Tool(
            "inspect_widget",
            "读取当前已打开的下拉/级联/树/日历弹层。含主页面选项，以及 layui iframe 内选项（source=iframe）。先观察再决策点击。",
            empty,
            self.inspect_widget,
        ))
        registry.register(Tool(
            "click_visible_text",
            "按可见文案点击选项（页面或弹层 iframe）。复杂选择器优先：open_field → inspect_widget → click_visible_text → confirm_overlay。",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要点击的可见文案，如 浙江省 / 温州市 / 确定"},
                    "where": {
                        "type": "string",
                        "enum": ["auto", "page", "iframe"],
                        "description": "auto 优先 iframe；page 仅主文档；iframe 仅弹层 iframe",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            self.click_visible_text,
        ))
        registry.register(Tool(
            "confirm_overlay",
            "点击最上层弹层的「确定/确认」。地区选择在 iframe 内点完省市区后调用。",
            empty,
            self.confirm_overlay,
        ))
        registry.register(Tool(
            "fill_from_profile",
            "把一个本地资料路径的真实值填写到网页字段并回读验证。密码/验证码字段不可用。",
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
            "fill_text",
            "用推理出的文本填写空字段（资料无直接路径时）。例如由身份证推生日、由学校匹配省市。不要用于密码/验证码。",
            {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string", "description": "inspect_page 返回的字段 ID"},
                    "value": {"type": "string", "description": "要填入的文本或下拉选项文案"},
                    "reason": {"type": "string", "description": "简短说明推理依据，便于日志回顾"},
                },
                "required": ["field_id", "value"],
                "additionalProperties": False,
            },
            self.fill_text,
        ))
        registry.register(Tool(
            "request_missing_profile_fields",
            "当必填项无法从现有资料填写时，在终端向用户逐项索取，并写回 profile.json 与 profile.json.template。",
            {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "description": "待补充项列表",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "页面字段中文名"},
                                "profile_path": {
                                    "type": "string",
                                    "description": "写入资料的路径，如 identity.political_status",
                                },
                                "hint": {"type": "string", "description": "给用户的补充说明"},
                            },
                            "required": ["label", "profile_path"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
            self.request_missing_profile_fields,
        ))
        registry.register(Tool(
            "dismiss_page_overlays",
            "关闭挡住点击的弹层/遮罩（如校验失败的 layui 提示），关闭后再 inspect_page。",
            empty,
            self.dismiss_page_overlays,
        ))
        registry.register(Tool(
            "fill_from_task_fact",
            "若任务说明里用“键: 值”列出了非敏感偏好，可用其填写字段；一般直接按自然语言任务说明决策即可。不要用于密码/验证码。",
            {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string"},
                    "fact_key": {"type": "string", "description": "任务说明中可选的短键名"},
                },
                "required": ["field_id", "fact_key"],
                "additionalProperties": False,
            },
            self.fill_from_task_fact,
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
            "便捷工具：按资料路径自动在弹层/iframe 中选择（地区省市区，或学校/专业等单关键字）。失败时改用 open_field + inspect_widget + click_visible_text + confirm_overlay。",
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
            "设置日期或年月字段。支持 yyyy-MM / yyyy-MM-dd；只读 WdatePicker/laydate 会尝试直接写入。入学年月用 education.enrollment_date，毕业年月用 education.graduation_date。",
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
            "attempt_auto_login",
            "临时高自动化登录：按任务说明选择招生项目、填账号密码；图形验证码先本地 OCR，失败再用视觉模型识别，并尝试点击登录；短信验证码仍需用户处理。不会把秘密返回给模型。",
            empty,
            self.attempt_auto_login,
        ))
        registry.register(Tool(
            "pause_for_user",
            "暂停 Agent，让用户在浏览器中输入验证码/短信码、完成登录或处理必须人工的步骤。用户可在终端输入自然语言指引。",
            {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            self.pause_for_user,
        ))
        return registry
