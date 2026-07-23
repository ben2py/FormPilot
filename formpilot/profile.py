from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


PROFILE_LABELS = {
    "identity.full_name_zh": "中文姓名",
    "identity.name_pinyin": "姓名拼音",
    "identity.document_type": "证件类型",
    "identity.document_number": "证件号码",
    "identity.gender": "性别",
    "identity.birth_date": "出生日期",
    "identity.nationality": "国籍/地区",
    "identity.ethnicity": "民族",
    "identity.marital_status": "婚姻状况",
    "identity.political_status": "政治面貌",
    "identity.military_status": "军人情况",
    "contact.mobile": "手机号码",
    "contact.email": "电子邮箱",
    "contact.address": "通信地址",
    "contact.postal_code": "邮政编码",
    "education.school": "毕业/在读院校",
    "education.major": "所学专业",
    "education.degree": "学位",
    "education.graduation_date": "毕业日期",
    "education.enrollment_date": "入学日期",
    "origin.province": "籍贯省份",
    "origin.city": "籍贯城市",
    "origin.district": "籍贯区县",
    "origin.hukou_location": "户籍所在地",
    "archive.unit": "档案所在单位",
    "archive.address": "档案单位地址",
    "archive.postal_code": "档案单位邮编",
    "application.project": "招生项目",
    "application.type": "报考类型",
    "application.batch": "招生批次",
    "application.mode": "招生方式",
    "application.program": "报考专业",
}

HIGH_RISK_PATHS = {
    "identity.birth_date",
    "identity.document_number",
    "contact.mobile",
    "contact.email",
    "contact.address",
    "origin.province",
    "origin.city",
    "origin.district",
    "origin.hukou_location",
    "archive.address",
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


def _set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise ValueError("empty profile path")
    current: dict[str, Any] = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(slots=True)
class ProfileStore:
    data: dict[str, Any]
    path: Path | None = None
    template_path: Path | None = None

    @classmethod
    def load(cls, path: str | Path, *, template_path: str | Path | None = None) -> "ProfileStore":
        profile_path = Path(path)
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Profile root must be a JSON object")
        resolved_template = Path(template_path) if template_path else profile_path.with_name("profile.json.template")
        return cls(raw, path=profile_path, template_path=resolved_template)

    def get(self, path: str) -> Any:
        current: Any = self.data
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise KeyError(f"Profile path does not exist: {path}")
            current = current[part]
        if current is None or current == "":
            raise KeyError(f"Profile path is empty: {path}")
        return current

    def set(self, path: str, value: Any) -> None:
        _set_nested(self.data, path, value)

    def update_many(self, updates: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        for path, value in updates.items():
            text = str(value).strip() if value is not None else ""
            if not text:
                continue
            self.set(path, text)
            changed.append(path)
        return changed

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("profile path is not configured")
        _write_json(target, self.data)
        self.path = target
        return target

    def sync_template(self, path: str | Path | None = None) -> Path:
        """Ensure template contains all keys; keep existing template example values when present."""
        target = Path(path) if path is not None else self.template_path
        if target is None:
            raise ValueError("template path is not configured")
        existing: dict[str, Any] = {}
        if target.exists():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except json.JSONDecodeError:
                existing = {}
        merged = json.loads(json.dumps(existing, ensure_ascii=False))
        for path, value in _flatten(self.data):
            try:
                current = merged
                parts = path.split(".")
                for part in parts[:-1]:
                    child = current.get(part)
                    if not isinstance(child, dict):
                        child = {}
                        current[part] = child
                    current = child
                if parts[-1] not in current or current[parts[-1]] in (None, ""):
                    current[parts[-1]] = value
            except Exception:
                _set_nested(merged, path, value)
        _write_json(target, merged)
        self.template_path = target
        return target

    def catalog(self, *, include_values: bool = True) -> list[dict[str, Any]]:
        """Return profile fields for the agent. Values are included so the model can infer."""
        result = []
        for path, value in _flatten(self.data):
            if value is None or value == "":
                continue
            item: dict[str, Any] = {
                "path": path,
                "label": PROFILE_LABELS.get(path, path.split(".")[-1]),
                "value_type": type(value).__name__,
                "risk": "high" if path in HIGH_RISK_PATHS else "normal",
            }
            if include_values:
                item["value"] = value
            result.append(item)
        return result

    def public_profile(self) -> dict[str, Any]:
        """Full profile object for model reasoning (local form-fill use)."""
        return json.loads(json.dumps(self.data, ensure_ascii=False))

    def masked_value(self, path: str) -> str:
        return _mask(path, self.get(path))
