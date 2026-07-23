from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


USERNAME_PATTERN = re.compile(r"用户名|账号|帐号|username|user(?:name)?|学号|工号|登录名|证件号", re.I)
PASSWORD_PATTERN = re.compile(r"密码|password|口令", re.I)
SMS_SECRET_PATTERN = re.compile(r"短信|手机验证码|动态码|otp", re.I)
IMAGE_CAPTCHA_PATTERN = re.compile(r"验证码|captcha", re.I)
HUMAN_SECRET_PATTERN = re.compile(r"验证码|captcha|短信码|动态码|手机验证码|otp|签名", re.I)
PROJECT_SELECT_PATTERN = re.compile(r"招生项目|报考项目|报名项目|kslb", re.I)


@dataclass(slots=True)
class LoginCredentials:
    username: str
    password: str
    source: str = "env"

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


def field_text(field: dict[str, Any]) -> str:
    return " ".join(str(field.get(key, "")) for key in ("label", "name", "id", "placeholder", "type"))


def is_username_field(field: dict[str, Any]) -> bool:
    if str(field.get("type", "")).lower() == "password":
        return False
    if HUMAN_SECRET_PATTERN.search(field_text(field)):
        return False
    return bool(USERNAME_PATTERN.search(field_text(field)))


def is_password_field(field: dict[str, Any]) -> bool:
    if HUMAN_SECRET_PATTERN.search(field_text(field)):
        return False
    return str(field.get("type", "")).lower() == "password" or bool(PASSWORD_PATTERN.search(field_text(field)))


def is_sms_secret_field(field: dict[str, Any]) -> bool:
    return bool(SMS_SECRET_PATTERN.search(field_text(field)))


def is_image_captcha_field(field: dict[str, Any]) -> bool:
    text = field_text(field)
    if SMS_SECRET_PATTERN.search(text):
        return False
    return bool(IMAGE_CAPTCHA_PATTERN.search(text))


def is_human_secret_field(field: dict[str, Any]) -> bool:
    """Secrets that still require a human after autofill/OCR attempts."""
    text = field_text(field)
    if SMS_SECRET_PATTERN.search(text):
        return True
    if str(field.get("type", "")).lower() in {"file"}:
        return True
    if re.search(r"签名", text, re.I):
        return True
    return False


def is_project_select_field(field: dict[str, Any]) -> bool:
    return str(field.get("tag", "")).lower() == "select" and bool(PROJECT_SELECT_PATTERN.search(field_text(field)))


def match_select_option(options: list[dict[str, Any]], wanted_raw: Any) -> dict[str, Any] | None:
    """Match select options conservatively to avoid short tokens picking the wrong item."""
    wanted = re.sub(r"[\s_\-—:：]", "", str(wanted_raw or "")).lower()
    if not wanted or wanted in {"-1", "请选择", "-请选择招生项目-"}:
        return None

    def norm(text: Any) -> str:
        return re.sub(r"[\s_\-—:：]", "", str(text or "")).lower()

    exact = next(
        (
            option
            for option in options
            if norm(option.get("text")) == wanted or norm(option.get("value")) == wanted
        ),
        None,
    )
    if exact is not None:
        return exact

    contained = [
        option
        for option in options
        if (label := norm(option.get("text")))
        and label not in {"-请选择招生项目-", "请选择"}
        and label in wanted
    ]
    if len(contained) == 1:
        return contained[0]
    if contained:
        return max(contained, key=lambda option: len(norm(option.get("text"))))

    partial = []
    for option in options:
        label = norm(option.get("text"))
        if not label or label.startswith("-请选择"):
            continue
        if wanted in label and len(wanted) >= max(4, int(len(label) * 0.6)):
            partial.append(option)
    if len(partial) == 1:
        return partial[0]
    if partial:
        return max(partial, key=lambda option: len(norm(option.get("text"))))
    return None


def match_project_option(options: list[dict[str, Any]], guidance: str) -> dict[str, Any] | None:
    """Pick the enrollment-project option that best matches natural-language task text."""
    text = str(guidance or "")
    normalized_guide = re.sub(r"[\s_\-—:：]", "", text).lower()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for option in options:
        label = str(option.get("text") or "").strip()
        if not label or label.startswith("-请选择") or option.get("disabled"):
            continue
        compact = re.sub(r"[\s_\-—:：]", "", label).lower()
        if compact and compact in normalized_guide:
            candidates.append((len(compact), option))
            continue
        # Soft aliases for push/recommendation programs.
        if any(token in normalized_guide for token in ("推免", "预推免", "推免生")) and any(
            token in compact for token in ("推免", "预推免")
        ):
            candidates.append((len(compact) + 10, option))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


@dataclass(slots=True)
class LoginCredentials:
    username: str
    password: str
    source: str = "env"

    @property
    def configured(self) -> bool:
        return bool(self.username and self.password)


def field_text(field: dict[str, Any]) -> str:
    return " ".join(str(field.get(key, "")) for key in ("label", "name", "id", "placeholder", "type"))


def is_username_field(field: dict[str, Any]) -> bool:
    if str(field.get("type", "")).lower() == "password":
        return False
    if HUMAN_SECRET_PATTERN.search(field_text(field)):
        return False
    return bool(USERNAME_PATTERN.search(field_text(field)))


def is_password_field(field: dict[str, Any]) -> bool:
    if HUMAN_SECRET_PATTERN.search(field_text(field)):
        return False
    return str(field.get("type", "")).lower() == "password" or bool(PASSWORD_PATTERN.search(field_text(field)))


def is_human_secret_field(field: dict[str, Any]) -> bool:
    text = field_text(field)
    if HUMAN_SECRET_PATTERN.search(text):
        return True
    return str(field.get("type", "")).lower() in {"file"}


def load_login_credentials(*, page_url: str | None = None) -> LoginCredentials | None:
    """Load temporary login secrets from env or local credentials.json. Never log values."""
    username = (os.getenv("FORMPILOT_LOGIN_USERNAME") or "").strip()
    password = (os.getenv("FORMPILOT_LOGIN_PASSWORD") or "").strip()
    if username and password:
        return LoginCredentials(username=username, password=password, source="env")

    path = Path(os.getenv("FORMPILOT_CREDENTIALS_FILE", "credentials.json"))
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    host = urlparse(page_url or "").hostname or ""
    chosen: dict[str, Any] | None = None
    source = "credentials.json"
    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        hint = str(entry.get("base_url_hint") or key or "").lower()
        if host and hint and hint in host:
            chosen = entry
            source = f"credentials.json:{key}"
            break
    if chosen is None and isinstance(payload.get("default"), dict):
        chosen = payload["default"]
        source = "credentials.json:default"
    if chosen is None:
        return None
    username = str(chosen.get("username") or "").strip()
    password = str(chosen.get("password") or "").strip()
    if not username or not password:
        return None
    return LoginCredentials(username=username, password=password, source=source)
