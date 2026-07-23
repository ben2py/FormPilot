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
from ..documents import (
    list_local_documents,
    merge_pdfs,
    preview_pdf_text,
    suggest_documents_for_requirement,
)
from ..policy import ApprovalPolicy, LOGIN_CONTROL_PATTERN
from ..profile import PROFILE_LABELS, ProfileStore
from ..task import TaskBrief
from .base import Tool, ToolRegistry


NEXT_STEP_PATTERN = re.compile(r"下一步|继续|下一页|保存并下一步", re.I)
SKIP_REQUIRED_TYPES = {"hidden", "submit", "button", "reset", "image", "password"}
PHOTO_CONFIRM_PATTERN = re.compile(r"确认上传|开始上传", re.I)
ADD_ROW_PATTERN = re.compile(
    r"新增|添加|增加|增行|加一行|添加一行|增加一行|新增一行|添加成员|增加成员|新增成员|添加家庭成员|新增家庭成员",
    re.I,
)
FILE_PICK_PATTERN = re.compile(r"^(选择|浏览|选择文件|选择照片)$", re.I)


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
MissingHandler = Callable[[list[dict[str, Any]]], dict[str, str] | Awaitable[dict[str, str]]]


async def _default_confirm(message: str) -> bool:
    if os.getenv("FORMPILOT_AUTO_APPROVE", "").strip() in {"1", "true", "yes", "y"}:
        print(f"\n{message}\n[AUTO] 已自动允许", flush=True)
        return True
    answer = await asyncio.to_thread(input, f"\n{message}\n允许？[y/N] ")
    return answer.strip().lower() in {"y", "yes", "是", "允许"}


async def _default_pause(message: str) -> str:
    auto = os.getenv("FORMPILOT_AUTO_APPROVE", "").strip().lower() in {"1", "true", "yes", "y"}
    # File upload / signature / missing materials still need a human even in auto mode.
    needs_human = bool(
        re.search(r"上传|照片|签名|文件|材料|缺失|找不到|不确定|合并|短信|验证码", str(message or ""))
    )
    if auto and not needs_human:
        print(f"\n{message}\n[AUTO] 跳过人工暂停，继续执行", flush=True)
        return ""
    if auto and needs_human:
        print(f"\n{message}\n[AUTO] 该项需要人工处理，仍将等待你在终端确认…", flush=True)
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
        missing: MissingHandler | None = None,
        profile_path: str | Path | None = None,
        template_path: str | Path | None = None,
    ) -> None:
        self.browser = browser
        self.profile = profile
        self.task = task
        self.policy = policy or ApprovalPolicy()
        self.confirm_handler = confirm or _default_confirm
        self.pause_handler = pause or _default_pause
        self.missing_handler = missing
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
        if sanitized.get("table_row") is not None:
            sanitized["table_row"] = sanitized.get("table_row")
            sanitized["column_header"] = sanitized.get("column_header")
            sanitized["fill_hint"] = (
                "家庭成员表：优先 fill_family_from_profile 一次填完 profile.family 全部成员；"
                "也可按「姓名（成员1）」等标签 fill_from_profile（family.member1.name 等）"
            )
        if str(sanitized.get("type") or "").lower() == "file":
            sanitized["fill_hint"] = (
                "材料上传：先 suggest_documents_for_requirement(页面要求文案) → "
                "自信则 upload_local_file / upload_from_profile（务必传 requirement=网页要求原文）；"
                "多份合并用 merge_pdfs + preview_pdf_text 自检；不确定则 pause_for_user"
            )
            if sanitized.get("page_hint"):
                sanitized["page_hint"] = str(sanitized.get("page_hint"))[:280]
            if not sanitized.get("has_value"):
                sanitized["disabled"] = False
        else:
            sanitized.pop("page_hint", None)
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
            # After「确认上传」, many sites clear the file input but the photo is already stored.
            if str(field.get("type") or "").lower() == "file":
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

    @staticmethod
    def _normalize_field_value(field: dict[str, Any], value: Any) -> tuple[Any, str | None]:
        """Normalize values for site-specific validators. Returns (value, error)."""
        text = str(value or "").strip()
        label = str(field.get("label") or "")
        if re.search(r"绩点", label):
            text = text.replace("／", "/").replace("⁄", "/")
            text = re.sub(r"\s*/\s*", "/", text)
            if re.fullmatch(r"\d+(\.\d+)?", text):
                return text, "绩点须为「成绩/满分」格式（例 4.1/5 或 4.2/5），不要只填 4.1"
            if not re.fullmatch(r"\d+(\.\d+)?/\d+(\.\d+)?", text):
                return text, "绩点格式无效，请用 4.1/5 这种「成绩/满分」"
            return text, None
        return value, None

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

        value, norm_error = self._normalize_field_value(field, value)
        if norm_error:
            return {"ok": False, "error": norm_error, "profile_path": profile_path}

        result = await self.browser.fill(field_id, value)
        verification = result.get("verification")
        if isinstance(verification, dict):
            verification.pop("actual", None)
        result["profile_path"] = profile_path
        result["value_privacy"] = "已使用本地资料填写；验证信息中不含原值回显"
        return result

    def _family_members(self) -> list[tuple[str, dict[str, Any]]]:
        family = self.profile.data.get("family")
        if not isinstance(family, dict):
            return []
        items: list[tuple[int, str, dict[str, Any]]] = []
        for key, value in family.items():
            if not isinstance(value, dict):
                continue
            match = re.search(r"(\d+)$", str(key))
            order = int(match.group(1)) if match else 10_000
            items.append((order, str(key), value))
        items.sort(key=lambda item: (item[0], item[1]))
        return [(f"family.{key}", member) for _order, key, member in items]

    @staticmethod
    def _family_column_key(column_header: str, label: str) -> str | None:
        text = f"{column_header or ''} {label or ''}"
        if re.search(r"姓名|名字", text):
            return "name"
        if re.search(r"关系|称谓", text):
            return "relation"
        if re.search(r"单位|工作|职务|职业", text):
            return "work_unit"
        if re.search(r"电话|手机|联系", text):
            return "phone"
        return None

    def _family_row_map(self, fields: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
        family_fields = [
            item for item in fields
            if item.get("table_row") is not None
            or re.search(r"成员\d+|姓名|关系|称谓|工作单位|电话|手机", str(item.get("label") or ""))
        ]
        row_map: dict[int, list[dict[str, Any]]] = {}
        for field in family_fields:
            row = field.get("table_row")
            if row is None:
                match = re.search(r"成员\s*(\d+)", str(field.get("label") or ""))
                row = int(match.group(1)) if match else None
            if row is None:
                continue
            row_map.setdefault(int(row), []).append(field)
        if row_map:
            return row_map
        candidates = [
            item for item in fields
            if re.search(r"姓名|关系|称谓|单位|职务|电话|手机", str(item.get("label") or ""))
            and str(item.get("type") or "").lower() not in {"hidden", "file", "password"}
        ]
        width = 4
        for index, field in enumerate(candidates):
            row_map.setdefault(index // width + 1, []).append(field)
        return row_map

    def _find_add_row_control(self, controls: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [
            item for item in controls
            if isinstance(item, dict)
            and not item.get("disabled")
            and ADD_ROW_PATTERN.search(str(item.get("label") or ""))
            and not re.search(r"删除|移除|清空", str(item.get("label") or ""))
        ]
        if not candidates:
            return None
        # Prefer labels that mention family/member/row.
        ranked = sorted(
            candidates,
            key=lambda item: (
                0 if re.search(r"成员|家庭|行", str(item.get("label") or "")) else 1,
                len(str(item.get("label") or "")),
            ),
        )
        return ranked[0]

    async def _ensure_family_rows(self, needed: int) -> dict[str, Any]:
        """Click「新增/添加」until the family table has enough rows for profile members."""
        added = 0
        last_rows = 0
        for _ in range(max(0, needed) + 2):
            snapshot = await self.browser.inspect()
            row_map = self._family_row_map(snapshot.get("fields") or [])
            last_rows = len(row_map)
            if last_rows >= needed:
                return {"ok": True, "rows": last_rows, "added": added}
            control = self._find_add_row_control(snapshot.get("controls") or [])
            if control is None:
                return {
                    "ok": False,
                    "rows": last_rows,
                    "added": added,
                    "error": "页面行数不足且未找到「新增/添加」按钮",
                }
            await self.browser.click(str(control["control_id"]))
            added += 1
            await self.browser.wait_and_rescan(500)
        snapshot = await self.browser.inspect()
        row_map = self._family_row_map(snapshot.get("fields") or [])
        return {
            "ok": len(row_map) >= needed,
            "rows": len(row_map),
            "added": added,
            "error": None if len(row_map) >= needed else "点击新增后行数仍不足",
        }

    async def fill_family_from_profile(self, approval_id: str | None = None) -> dict[str, Any]:
        """Fill every family.* member from profile into the on-page member table."""
        members = self._family_members()
        if not members:
            return {
                "ok": False,
                "error": "profile.family 为空；请先在 profile.json 写好全部家庭成员",
            }
        ensure = await self._ensure_family_rows(len(members))
        snapshot = await self.browser.inspect()
        row_map = self._family_row_map(snapshot.get("fields") or [])
        if not row_map:
            return {
                "ok": False,
                "error": "当前页未识别到家庭成员表格字段；请 inspect_page 确认是否在「家庭主要成员」页",
                "members_in_profile": [path for path, _ in members],
                "ensure_rows": ensure,
            }

        filled: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        used_rows = sorted(row_map.keys())
        for member_index, (member_path, member) in enumerate(members):
            if member_index >= len(used_rows):
                errors.append({
                    "member": member_path,
                    "error": f"页面只有 {len(used_rows)} 行，无法填入第 {member_index + 1} 位成员",
                })
                continue
            row_no = used_rows[member_index]
            for field in row_map[row_no]:
                col_key = self._family_column_key(
                    str(field.get("column_header") or ""),
                    str(field.get("label") or ""),
                )
                if not col_key:
                    continue
                value = member.get(col_key)
                if value is None or str(value).strip() == "":
                    errors.append({
                        "member": member_path,
                        "field_id": field.get("field_id"),
                        "column": col_key,
                        "error": f"profile 缺少 {member_path}.{col_key}",
                    })
                    continue
                if self.policy.secret_field(field):
                    errors.append({
                        "member": member_path,
                        "field_id": field.get("field_id"),
                        "error": "该字段被策略视为秘密字段，已跳过",
                    })
                    continue
                result = await self.browser.fill(str(field["field_id"]), value)
                verification = result.get("verification")
                if isinstance(verification, dict):
                    verification.pop("actual", None)
                entry = {
                    "ok": bool(result.get("ok")),
                    "member": member_path,
                    "profile_path": f"{member_path}.{col_key}",
                    "field_id": field.get("field_id"),
                    "label": field.get("label"),
                    "table_row": row_no,
                    "column": col_key,
                    "verification": verification,
                    "error": result.get("error"),
                }
                (filled if entry["ok"] else errors).append(entry)

        ok = bool(filled) and not errors
        return {
            "ok": ok,
            "filled_count": len(filled),
            "error_count": len(errors),
            "members_in_profile": [path for path, _ in members],
            "rows_on_page": used_rows,
            "rows_added": ensure.get("added"),
            "ensure_rows": ensure,
            "filled": filled,
            "errors": errors or None,
            "value_privacy": "已按 profile.family 全部成员写入页面；结果不回传原值",
            "hint": (
                "若还有空行或 errors，inspect_page 后对缺项 fill_from_profile；"
                "确认成员都填完再点下一步。"
            ),
        }

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
        text, norm_error = self._normalize_field_value(field, text)
        if norm_error:
            return {"ok": False, "error": norm_error, "field_id": field_id}
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

        paths = list(profile_paths)
        # Tongji school picker is province → university. Expand a lone school path.
        if paths == ["education.school"]:
            for province_path in ("education.province", "archive.province"):
                try:
                    self.profile.get(province_path)
                except KeyError:
                    continue
                paths = [province_path, "education.school"]
                break
        elif paths == ["education.major"]:
            for category_path in ("education.major_category", "education.degree_category"):
                try:
                    self.profile.get(category_path)
                except KeyError:
                    continue
                paths = [category_path, "education.major"]
                break

        try:
            values = [self.profile.get(path) for path in paths]
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        result = await self.browser.select_cascade(field_id, values)
        # Fallback: school-only keyword search if province→school failed.
        if (
            not result.get("ok")
            and paths != list(profile_paths)
            and list(profile_paths) == ["education.school"]
        ):
            try:
                school_only = [self.profile.get("education.school")]
            except KeyError as exc:
                return {"ok": False, "error": str(exc), "prior": result}
            retry = await self.browser.select_cascade(field_id, school_only)
            retry["profile_paths"] = profile_paths
            retry["tried_paths"] = paths
            retry["fallback"] = "school_only"
            retry.pop("actual", None)
            retry["value_privacy"] = "已使用本地资料逐级填写"
            if retry.get("ok"):
                return retry
            result["fallback_result"] = {k: retry.get(k) for k in ("ok", "error", "wanted", "available_options") if k in retry}
        result["profile_paths"] = paths
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

    def _attach_upload_context(
        self,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any],
        target: dict[str, Any] | None,
        local_path: str,
        profile_path: str | None = None,
        requirement: str | None = None,
    ) -> dict[str, Any]:
        """Attach webpage + file metadata so RunLogger can persist an upload event."""
        page_hint = ""
        if target:
            page_hint = str(target.get("page_hint") or "").strip()
        if not page_hint:
            # Fallback: short page text from snapshot if browser provided it.
            page_hint = str(snapshot.get("page_text") or snapshot.get("hint") or "").strip()[:280]
        field_label = str((target or {}).get("label") or "").strip() or "文件上传"
        req = str(requirement or "").strip() or None
        if not req and page_hint:
            # Prefer a compact slice of nearby page text as the declared webpage requirement.
            req = page_hint[:160]
        result["page_url"] = snapshot.get("url")
        result["page_title"] = snapshot.get("title")
        result["field_label"] = field_label
        result["field_name"] = (target or {}).get("name") or (target or {}).get("id")
        result["field_accept"] = (target or {}).get("accept") or ""
        result["page_hint"] = page_hint or None
        result["local_path"] = str(local_path)
        if profile_path:
            result["profile_path"] = profile_path
        if req:
            result["requirement"] = req
        result["upload_log"] = {
            "file": result.get("file_name") or Path(local_path).name,
            "local_path": str(local_path),
            "profile_path": profile_path,
            "page_label": field_label,
            "requirement": req,
            "page_url": snapshot.get("url"),
            "page_title": snapshot.get("title"),
            "ok": bool(result.get("ok")),
        }
        return result

    async def upload_from_profile(
        self,
        profile_path: str,
        field_id: str | None = None,
        approval_id: str | None = None,
        requirement: str | None = None,
    ) -> dict[str, Any]:
        """Upload a local file referenced by profile path onto a file input."""
        snapshot = await self.browser.inspect()
        fields = snapshot.get("fields") or []
        target = None
        if field_id:
            target = next((item for item in fields if item.get("field_id") == field_id), None)
            if target is None:
                return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        else:
            file_fields = [item for item in fields if str(item.get("type") or "").lower() == "file"]
            if not file_fields:
                return {"ok": False, "error": "当前页面没有 file 输入框"}
            empty = [item for item in file_fields if not item.get("has_value")]
            target = (empty or file_fields)[0]
            field_id = str(target.get("field_id"))
        try:
            rel = str(self.profile.get(profile_path))
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        if not hasattr(self.browser, "upload_file"):
            return {"ok": False, "error": "当前浏览器不支持 upload_file"}
        result = await self.browser.upload_file(field_id, rel)
        result["value_privacy"] = "已使用本地文件路径上传；结果不回传文件内容"
        return self._attach_upload_context(
            result,
            snapshot=snapshot,
            target=target,
            local_path=rel,
            profile_path=profile_path,
            requirement=requirement,
        )

    async def list_local_documents(self) -> dict[str, Any]:
        docs = list_local_documents()
        return {
            "ok": True,
            "count": len(docs),
            "documents": docs,
            "hint": (
                "上传材料页优先 upload_materials_from_profile（会扫表并上传全部可匹配项，"
                "含可选的外国语水平能力证明→英语成绩证明.pdf）；"
                "不要漏掉可选但本地有文件的材料。"
            ),
        }

    async def suggest_documents_for_requirement(self, requirement: str) -> dict[str, Any]:
        return suggest_documents_for_requirement(requirement)

    async def merge_pdfs(self, paths: list[str], output_name: str | None = None) -> dict[str, Any]:
        return merge_pdfs(paths, output_name=output_name)

    async def preview_pdf_text(self, path: str, max_chars: int = 4000) -> dict[str, Any]:
        return preview_pdf_text(path, max_chars=max(500, min(int(max_chars or 4000), 12000)))

    async def scan_material_rows(self) -> dict[str, Any]:
        await self.browser.inspect()  # ensure data-formpilot-id attributes exist
        if hasattr(self.browser, "scan_material_rows"):
            return await self.browser.scan_material_rows()
        return {"ok": False, "error": "当前浏览器未实现 scan_material_rows"}

    async def upload_materials_from_profile(self, approval_id: str | None = None) -> dict[str, Any]:
        """Scan 上传材料 table and upload every row that has a confident local match.

        Includes optional rows (e.g. 外国语水平能力证明) when a local PDF matches.
        """
        await self.browser.inspect()
        if not hasattr(self.browser, "scan_material_rows"):
            return {"ok": False, "error": "当前浏览器未实现 scan_material_rows"}
        scanned = await self.browser.scan_material_rows()
        materials = list(scanned.get("materials") or [])
        if not materials:
            return {
                "ok": False,
                "error": "未识别到上传材料表格；请确认当前在「上传材料」页",
                "hint": "先 inspect_page，再调用本工具",
            }

        uploaded: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for item in materials:
            requirement = str(item.get("requirement") or "").strip()
            field_id = str(item.get("field_id") or "").strip()
            if item.get("uploaded") or item.get("status") == "已上传":
                skipped.append({
                    "requirement": requirement,
                    "field_id": field_id,
                    "reason": "页面已显示已上传",
                })
                continue
            if not field_id:
                failed.append({"requirement": requirement, "error": "无 file 字段 id"})
                continue

            suggestion = suggest_documents_for_requirement(requirement)
            matches = list(suggestion.get("matches") or [])
            # Hard fallback for language proof ↔ documents.english / 英语成绩证明.
            if not matches and re.search(r"外国语|外语|英语|语言.*证明|CET|IELTS|TOEFL", requirement, re.I):
                try:
                    eng = str(self.profile.get("documents.english"))
                    matches = [{"path": eng, "name": Path(eng).name, "score": 100}]
                    suggestion = {**suggestion, "confident": True, "matches": matches}
                except KeyError:
                    pass

            if not matches:
                if item.get("required"):
                    failed.append({
                        "requirement": requirement,
                        "field_id": field_id,
                        "error": "必填但无本地自信匹配；请 pause_for_user",
                    })
                else:
                    skipped.append({
                        "requirement": requirement,
                        "field_id": field_id,
                        "reason": "可选且无本地匹配",
                    })
                continue

            is_language = bool(re.search(r"外国语|外语|英语|语言", requirement))
            top = matches[0]
            if not suggestion.get("confident") and not is_language:
                if item.get("required"):
                    failed.append({
                        "requirement": requirement,
                        "field_id": field_id,
                        "error": "匹配不自信，需人工确认",
                        "candidates": [m.get("name") for m in matches[:3]],
                    })
                else:
                    skipped.append({
                        "requirement": requirement,
                        "reason": "可选且匹配不自信",
                        "candidates": [m.get("name") for m in matches[:3]],
                    })
                continue

            path = str(top.get("path") or "")
            if not path:
                failed.append({"requirement": requirement, "error": "候选无路径"})
                continue
            if not hasattr(self.browser, "upload_file"):
                return {"ok": False, "error": "当前浏览器不支持 upload_file"}
            result = await self.browser.upload_file(field_id, path)
            entry = {
                "requirement": requirement,
                "field_id": field_id,
                "path": path,
                "file_name": Path(path).name,
                "required": bool(item.get("required")),
                "ok": bool(result.get("ok")),
                "uploaded_status": result.get("uploaded_status"),
                "page_messages": result.get("page_messages"),
                "error": result.get("error"),
                "upload_log": {
                    "file": Path(path).name,
                    "local_path": path,
                    "requirement": requirement,
                    "ok": bool(result.get("ok")),
                },
            }
            if entry["ok"]:
                uploaded.append(entry)
            else:
                failed.append(entry)
            await self.browser.wait_and_rescan(400)

        # Re-scan to report remaining gaps (especially language proof).
        await self.browser.inspect()
        after = await self.browser.scan_material_rows()
        pending = [
            {
                "requirement": m.get("requirement"),
                "required": m.get("required"),
                "status": m.get("status"),
            }
            for m in (after.get("materials") or [])
            if not m.get("uploaded")
        ]
        language_pending = [
            p for p in pending
            if re.search(r"外国语|外语|英语|语言", str(p.get("requirement") or ""))
        ]
        ok = not failed and not language_pending
        return {
            "ok": ok,
            "uploaded_count": len(uploaded),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "uploaded": uploaded,
            "skipped": skipped,
            "failed": failed or None,
            "pending_after": pending,
            "language_pending": language_pending or None,
            "hint": (
                "若 language_pending 非空，必须再上传 documents.english / 英语成绩证明.pdf；"
                "required 的 failed 需 pause_for_user；全部完成后再点下一步。"
                if language_pending or failed
                else "材料行已尽量上传（含可选外国语证明）；可 inspect 确认后下一步。"
            ),
        }

    async def upload_local_file(
        self,
        path: str,
        field_id: str | None = None,
        approval_id: str | None = None,
        requirement: str | None = None,
    ) -> dict[str, Any]:
        """Upload an explicit local file path onto a file input."""
        snapshot = await self.browser.inspect()
        fields = snapshot.get("fields") or []
        target = None
        if field_id:
            target = next((item for item in fields if item.get("field_id") == field_id), None)
            if target is None:
                return {"ok": False, "error": "字段已消失，请重新 inspect_page"}
        else:
            file_fields = [item for item in fields if str(item.get("type") or "").lower() == "file"]
            if not file_fields:
                return {"ok": False, "error": "当前页面没有 file 输入框"}
            empty = [item for item in file_fields if not item.get("has_value")]
            target = (empty or file_fields)[0]
            field_id = str(target.get("field_id"))
        if not hasattr(self.browser, "upload_file"):
            return {"ok": False, "error": "当前浏览器不支持 upload_file"}
        result = await self.browser.upload_file(field_id, path)
        result["value_privacy"] = "已使用本地文件路径上传；结果不回传文件内容"
        return self._attach_upload_context(
            result,
            snapshot=snapshot,
            target=target,
            local_path=str(path),
            requirement=requirement,
        )
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
                        "年月用 set_date_from_profile；家庭用 fill_family_from_profile；"
                        "材料用 upload_materials_from_profile（含可选外国语证明）；"
                        "失败则 open_field → inspect_widget → "
                        "click_visible_text → confirm_overlay；资料不足则 request_missing_profile_fields。"
                    ),
                }
            # Materials page: do not leave while 外国语证明 is still 未上传 and we have the PDF.
            file_fields = [
                f for f in (snapshot.get("fields") or [])
                if str(f.get("type") or "").lower() == "file"
            ]
            if len(file_fields) >= 2 and hasattr(self.browser, "scan_material_rows"):
                try:
                    scanned = await self.browser.scan_material_rows()
                except Exception:
                    scanned = {}
                language_pending = [
                    m for m in (scanned.get("materials") or [])
                    if not m.get("uploaded")
                    and re.search(r"外国语|外语|英语|语言", str(m.get("requirement") or ""))
                ]
                has_english = False
                try:
                    eng = str(self.profile.get("documents.english") or "")
                    has_english = bool(eng) and Path(eng).expanduser().exists()
                except KeyError:
                    has_english = any(
                        Path("personal_info/PDF/英语成绩证明.pdf").exists(),
                    )
                if language_pending and has_english:
                    return {
                        "ok": False,
                        "blocked": True,
                        "error": "外国语水平能力证明仍未上传，禁止下一步",
                        "language_pending": [
                            {"requirement": m.get("requirement"), "field_id": m.get("field_id")}
                            for m in language_pending
                        ],
                        "hint": (
                            "请先 upload_materials_from_profile，或 upload_local_file "
                            "path=personal_info/PDF/英语成绩证明.pdf / documents.english"
                        ),
                    }
        target = f"click:{snapshot['url']}:{control_id}"
        requires_confirm = self.policy.click_requires_confirmation(control)
        # Local photo confirm is part of the authorized upload flow.
        if PHOTO_CONFIRM_PATTERN.search(label):
            requires_confirm = False
        # Adding table rows (family/experience) is routine form editing — no human gate.
        if ADD_ROW_PATTERN.search(label):
            requires_confirm = False
        # Material/photo "选择" buttons are part of authorized local upload flow.
        if FILE_PICK_PATTERN.search(label):
            requires_confirm = False
        if requires_confirm and not self.policy.consume(approval_id, target):
            return {
                "ok": False,
                "confirmation_required": True,
                "target": target,
                "summary": f"在 {snapshot['url']} 点击“{control['label']}”",
            }
        result = await self.browser.click(control_id)
        messages = list(result.get("page_messages") or []) + list(result.get("dialogs") or [])
        joined = " ".join(str(m) for m in messages)
        if NEXT_STEP_PATTERN.search(label) and not result.get("url_changed") and re.search(
            r"保存失败|校验|不能为空|请选择|格式", joined
        ):
            result["ok"] = False
            result["blocked"] = True
            result["error"] = joined[:200] or "保存失败，页面未前进"
            result["hint"] = (
                "服务器保存失败：不要 dismiss_page_overlays。"
                "先 search_visible_text「校验结果」；"
                "学习信息常见原因：专业未真正选中、年月非 yyyy-MM、"
                "排名名次不要写成 3/94（名次与总人数分两栏）、"
                "绩点须为 4.1/5 这种成绩/满分（禁止只填 4.1）。"
                "修好后 confirm_overlay 点确定，再点下一步。"
            )
        return result

    async def dismiss_page_overlays(self) -> dict[str, Any]:
        """Close common modal/shade layers that block clicks (e.g. layui)."""
        # Refuse to wipe the only clue after a failed save.
        if hasattr(self.browser, "page") and self.browser.page is not None:
            try:
                visible = await self.browser.page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                      '.layui-layer-msg, .layui-layer-dialog .layui-layer-content, [role=\"alert\"]'
                    )).map(n => String(n.innerText||n.textContent||'').replace(/\\s+/g,' ').trim())
                      .filter(Boolean).slice(0, 8)"""
                )
            except Exception:
                visible = []
            joined = " ".join(str(m) for m in (visible or []))
            if re.search(r"保存失败|校验失败|不能为空", joined):
                return {
                    "ok": False,
                    "blocked": True,
                    "dismissed": 0,
                    "closed_messages": list(visible or [])[:8],
                    "error": "当前弹层含保存/校验失败提示，禁止关闭",
                    "hint": (
                        "请先根据 closed_messages/校验结果修字段；"
                        "绩点用 4.1/5 格式；修好后用 confirm_overlay 点确定关掉弹窗，再点下一步。"
                        "不要清空错误提示后盲点下一步。"
                    ),
                }
        if hasattr(self.browser, "dismiss_page_overlays"):
            result = await self.browser.dismiss_page_overlays()  # type: ignore[misc]
        elif hasattr(self.browser, "page") and self.browser.page is not None:
            result = await self.browser.close_layui_layers()
            result = {"ok": True, "dismissed": int(result or 0), "messages": []}
        else:
            return {"ok": True, "dismissed": 0, "messages": []}
        await self.browser.wait_and_rescan(400)
        public = self._public_snapshot(await self.browser.inspect())
        public["dismissed"] = int((result or {}).get("dismissed") or 0)
        public["closed_messages"] = list((result or {}).get("messages") or [])[:8]
        public["message"] = f"已尝试关闭弹层 {public['dismissed']} 个"
        if public["closed_messages"]:
            public["message"] += "；关闭前可见文案已放入 closed_messages"
        return public

    async def request_missing_profile_fields(self, fields: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask the human for missing profile values (UI or terminal), then persist them."""
        if not fields:
            return {"ok": False, "error": "fields 不能为空"}

        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(fields, start=1):
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or item.get("field_label") or f"字段{index}").strip()
            path = str(item.get("profile_path") or item.get("path") or "").strip()
            hint = str(item.get("hint") or item.get("reason") or "").strip()
            if not path:
                slug = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", label).strip("_") or f"field_{index}"
                path = f"extras.{slug}"
            normalized.append({"label": label, "profile_path": path, "hint": hint})

        updates: dict[str, str] = {}
        collected: list[dict[str, str]] = []

        if self.missing_handler is not None:
            reply = await self._call_maybe_async(self.missing_handler, normalized)
            values = reply if isinstance(reply, dict) else {}
            for item in normalized:
                path = item["profile_path"]
                label = item["label"]
                value = str(values.get(path) or values.get(label) or "").strip()
                if not value:
                    collected.append({"label": label, "profile_path": path, "status": "skipped"})
                    continue
                updates[path] = value
                collected.append({"label": label, "profile_path": path, "status": "saved"})
                if path not in PROFILE_LABELS:
                    PROFILE_LABELS[path] = label
        else:
            print("\n========== 需要补充个人资料 ==========", flush=True)
            print("以下必填/空缺项无法仅从现有资料可靠填写。请在终端逐项输入；直接回车表示跳过该项。", flush=True)
            for index, item in enumerate(normalized, start=1):
                label = item["label"]
                path = item["profile_path"]
                hint = item["hint"]
                prompt = f"[{index}/{len(normalized)}] {label} ({path})"
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

        if self.missing_handler is None:
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
            "fill_family_from_profile",
            "家庭主要成员页专用：把 profile.family 下全部成员一次性填入；行数不够会自动点「新增/添加」（无需人工确认）。不要只填一人。",
            {
                "type": "object",
                "properties": {
                    "approval_id": nullable_approval,
                },
                "required": ["approval_id"],
                "additionalProperties": False,
            },
            self.fill_family_from_profile,
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
            "upload_from_profile",
            "把本地资料中的文件路径上传到页面的 file 输入框（证件照用 documents.photo）。可省略 field_id；务必传 requirement=网页材料要求原文以便写入行动日志；上传后如有「确认上传」请再 click_control。",
            {
                "type": "object",
                "properties": {
                    "profile_path": {"type": "string", "description": "如 documents.photo"},
                    "field_id": {"type": ["string", "null"], "description": "可选；省略则自动选择本页 file 字段"},
                    "approval_id": nullable_approval,
                    "requirement": {
                        "type": ["string", "null"],
                        "description": "网页上的材料要求原文，如「证件照」「本科成绩单」；会写入 upload 日志",
                    },
                },
                "required": ["profile_path", "field_id", "approval_id", "requirement"],
                "additionalProperties": False,
            },
            self.upload_from_profile,
        ))
        registry.register(Tool(
            "list_local_documents",
            "列出 personal_info/ 与 personal_info/PDF/ 下可用的本地材料（PDF/图片路径与文件名）。进入上传材料页时先调用。",
            empty,
            self.list_local_documents,
        ))
        registry.register(Tool(
            "scan_material_rows",
            "扫描上传材料表格：每行的材料名、是否必填、已上传/未上传、对应 file 字段。",
            empty,
            self.scan_material_rows,
        ))
        registry.register(Tool(
            "upload_materials_from_profile",
            "上传材料页主工具：扫表后按网页要求匹配并上传本地 PDF；含可选的「外国语水平能力证明」→英语成绩证明.pdf。进入该页应优先调用，勿漏传。",
            {
                "type": "object",
                "properties": {"approval_id": nullable_approval},
                "required": ["approval_id"],
                "additionalProperties": False,
            },
            self.upload_materials_from_profile,
        ))
        registry.register(Tool(
            "suggest_documents_for_requirement",
            "根据网页上的上传要求文案，严格匹配本地文件名/内容关键词，返回候选与是否自信。上传前必须先对每条要求调用。",
            {
                "type": "object",
                "properties": {
                    "requirement": {
                        "type": "string",
                        "description": "网页上的材料名称或说明，如「身份证扫描件」「本科成绩单」",
                    },
                },
                "required": ["requirement"],
                "additionalProperties": False,
            },
            self.suggest_documents_for_requirement,
        ))
        registry.register(Tool(
            "merge_pdfs",
            "把多份本地 PDF 合并为一个文件（输出到 .formpilot/merged/）。当网页要求一份材料但本地是多份相关证明时使用；合并后必须 preview_pdf_text 自检。",
            {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 20,
                        "description": "按合并顺序的 PDF 相对/绝对路径",
                    },
                    "output_name": {
                        "type": ["string", "null"],
                        "description": "可选输出文件名（不含路径）",
                    },
                },
                "required": ["paths", "output_name"],
                "additionalProperties": False,
            },
            self.merge_pdfs,
        ))
        registry.register(Tool(
            "preview_pdf_text",
            "提取 PDF 文本供你核对是否覆盖网页要求的内容。合并后或上传前不确定时调用；扫描件可能无文本则 pause_for_user。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 500, "maximum": 12000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            self.preview_pdf_text,
        ))
        registry.register(Tool(
            "upload_local_file",
            "把明确的本地文件路径上传到页面 file 输入框（来自 suggest/merge 返回的 path）。务必传 requirement=网页要求原文（写入行动日志）；可省略 field_id；上传后若有「确认上传」再 click_control。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "如 personal_info/PDF/本科成绩单.pdf"},
                    "field_id": {"type": ["string", "null"]},
                    "approval_id": nullable_approval,
                    "requirement": {
                        "type": ["string", "null"],
                        "description": "网页材料要求原文；会写入 upload 日志",
                    },
                },
                "required": ["path", "field_id", "approval_id", "requirement"],
                "additionalProperties": False,
            },
            self.upload_local_file,
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
