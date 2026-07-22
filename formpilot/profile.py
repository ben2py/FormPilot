from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


PROFILE_LABELS = {
    "identity.full_name_zh": "中文姓名",
    "identity.document_type": "证件类型",
    "identity.document_number": "证件号码",
    "identity.gender": "性别",
    "identity.birth_date": "出生日期",
    "identity.nationality": "国籍/地区",
    "identity.ethnicity": "民族",
    "contact.mobile": "手机号码",
    "contact.email": "电子邮箱",
    "contact.address": "通信地址",
    "contact.postal_code": "邮政编码",
    "education.school": "毕业/在读院校",
    "education.major": "所学专业",
    "education.degree": "学位",
    "education.graduation_date": "毕业日期",
    "application.project": "招生项目",
    "application.batch": "招生批次",
    "application.mode": "招生方式",
    "application.program": "报考专业",
}

HIGH_RISK_PATHS = {
    "identity.document_number",
    "contact.mobile",
    "contact.email",
    "contact.address",
}


def _flatten(value: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            yield from _flatten(child, path)
    elif prefix:
        yield prefix, value


def _mask(path: str, value: Any) -> str:
    text = str(value)
    if path == "identity.document_number" and len(text) >= 8:
        return f"{text[:3]}••••••{text[-4:]}"
    if path == "contact.mobile" and len(text) >= 7:
        return f"{text[:3]}••••{text[-4:]}"
    if path == "contact.email" and "@" in text:
        name, domain = text.split("@", 1)
        return f"{name[:2]}•••@{domain}"
    if path == "contact.address" and len(text) > 8:
        return f"{text[:6]}••••"
    return text


@dataclass(slots=True)
class ProfileStore:
    data: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "ProfileStore":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Profile root must be a JSON object")
        return cls(raw)

    def get(self, path: str) -> Any:
        current: Any = self.data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"Profile path does not exist: {path}")
            current = current[part]
        if current is None or current == "":
            raise KeyError(f"Profile path is empty: {path}")
        return current

    def catalog(self) -> list[dict[str, Any]]:
        """Return semantic metadata without exposing raw high-risk values."""
        result = []
        for path, value in _flatten(self.data):
            if value is None or value == "":
                continue
            item = {
                "path": path,
                "label": PROFILE_LABELS.get(path, path.split(".")[-1]),
                "value_type": type(value).__name__,
                "risk": "high" if path in HIGH_RISK_PATHS else "normal",
            }
            result.append(item)
        return result

    def masked_value(self, path: str) -> str:
        return _mask(path, self.get(path))
