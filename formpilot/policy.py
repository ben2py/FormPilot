from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field

from .credentials import (
    is_human_secret_field,
    is_image_captcha_field,
    is_password_field,
    is_sms_secret_field,
    load_login_credentials,
)


SECRET_PATTERN = re.compile(r"验证码|captcha|短信码|动态码|密码|password|口令|签名", re.I)
SIDE_EFFECT_PATTERN = re.compile(r"提交|注册|登录|保存|发送|获取验证码|同意|确认|支付|删除|上传", re.I)
SAFE_NAVIGATION_PATTERN = re.compile(r"^(下一步|上一步|返回|继续|下一页|上一页)$")
# Form table edits (family/experience rows): do not bother the human.
SAFE_TABLE_EDIT_PATTERN = re.compile(
    r"^(新增|添加|增加|增行|加一行|添加一行|增加一行|新增一行|添加成员|增加成员|新增成员|"
    r"添加家庭成员|新增家庭成员|加号|\+|＋|选择|浏览|选择文件|选择照片)$|"
    r"新增|添加一行|增加一行|添加成员|新增成员|选择文件|选择照片",
    re.I,
)
LOGIN_URL_PATTERN = re.compile(r"logon|login|signin|sign-in|/sso\b|/auth\b", re.I)
LOGIN_CONTROL_PATTERN = re.compile(r"登录|登陆|login|sign\s*in", re.I)


def page_requires_user_pause(snapshot: dict) -> bool:
    """True when SMS/OTP or unresolved login secrets still need a human."""
    if not isinstance(snapshot, dict):
        return False
    url = str(snapshot.get("url", ""))
    fields = snapshot.get("fields") or []
    controls = snapshot.get("controls") or []

    empty_sms = any(
        is_sms_secret_field(field) and not field.get("has_value")
        for field in fields
        if isinstance(field, dict)
    )
    if empty_sms:
        return True

    empty_human = any(
        is_human_secret_field(field) and not field.get("has_value")
        for field in fields
        if isinstance(field, dict)
    )
    if empty_human:
        return True

    creds = load_login_credentials(page_url=url)
    empty_password = any(
        is_password_field(field) and not field.get("has_value")
        for field in fields
        if isinstance(field, dict)
    )
    if empty_password and not (creds and creds.configured):
        return True

    # Stay on a login page => enter autofill path (OCR/captcha) or pause for SMS.
    still_login = bool(LOGIN_URL_PATTERN.search(url)) or any(
        LOGIN_CONTROL_PATTERN.search(str(control.get("label", ""))) for control in controls if isinstance(control, dict)
    )
    return still_login


@dataclass(slots=True)
class ApprovalPolicy:
    approvals: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def secret_field(field: dict) -> bool:
        text = " ".join(str(field.get(key, "")) for key in ("label", "name", "id", "placeholder", "type"))
        return bool(SECRET_PATTERN.search(text)) or field.get("type") in {"password", "file"}

    @staticmethod
    def click_requires_confirmation(control: dict) -> bool:
        label = str(control.get("label", "")).strip()
        if SAFE_TABLE_EDIT_PATTERN.search(label):
            return False
        if SIDE_EFFECT_PATTERN.search(label):
            return True
        if SAFE_NAVIGATION_PATTERN.fullmatch(label):
            return False
        control_type = str(control.get("type", "")).lower()
        if control_type in {"link", "a"}:
            return False
        return True

    def issue(self, target: str) -> str:
        approval_id = secrets.token_urlsafe(18)
        self.approvals[approval_id] = target
        return approval_id

    def consume(self, approval_id: str | None, target: str) -> bool:
        if not approval_id or self.approvals.get(approval_id) != target:
            return False
        del self.approvals[approval_id]
        return True
