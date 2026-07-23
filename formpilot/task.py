from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_FACT_LINE = re.compile(r"^[-*]\s*([^:：]+)\s*[:：]\s*(.+?)\s*$")
_FACTS_HEADING = re.compile(r"^#{1,6}\s*(已知事实|facts|fact)\s*$", re.I)


@dataclass(slots=True)
class TaskBrief:
    """User-authored task document the agent can read for goals and non-secret facts."""

    path: Path
    text: str
    facts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TaskBrief":
        task_path = Path(path)
        if not task_path.exists():
            raise FileNotFoundError(f"任务文档不存在：{task_path}")
        text = task_path.read_text(encoding="utf-8")
        return cls(path=task_path, text=text, facts=_parse_facts(text))

    def public_view(self, *, max_chars: int = 12000) -> dict[str, Any]:
        body = self.text.strip()
        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars] + "\n\n…(任务说明已截断)"
        return {
            "ok": True,
            "path": str(self.path),
            "truncated": truncated,
            "content": body,
            "facts": [{"key": key, "value": value} for key, value in self.facts.items()],
            "note": "这是用户用自然语言写的任务说明；按其中目标与偏好自主执行，不要要求用户改写成技术格式",
        }

    def fact_catalog(self) -> list[dict[str, str]]:
        return [{"key": key, "value": value} for key, value in self.facts.items()]

    def get_fact(self, key: str) -> str:
        if key not in self.facts:
            raise KeyError(f"任务事实不存在：{key}")
        return self.facts[key]


def _parse_facts(text: str) -> dict[str, str]:
    facts: dict[str, str] = {}
    lines = text.splitlines()
    in_facts = False
    json_buffer: list[str] = []
    in_json = False

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if _FACTS_HEADING.match(stripped):
            in_facts = True
            in_json = False
            json_buffer = []
            continue
        if in_facts and stripped.startswith("#"):
            break
        if not in_facts:
            continue
        if stripped.startswith("```"):
            fence = stripped.strip("`").lower()
            if not in_json and (fence in {"", "json"}):
                in_json = True
                json_buffer = []
                continue
            if in_json:
                payload = "\n".join(json_buffer).strip()
                if payload:
                    try:
                        data = json.loads(payload)
                    except json.JSONDecodeError:
                        data = None
                    if isinstance(data, dict):
                        for key, value in data.items():
                            facts[str(key).strip()] = str(value).strip()
                in_json = False
                json_buffer = []
            continue
        if in_json:
            json_buffer.append(line)
            continue
        match = _FACT_LINE.match(stripped)
        if match:
            facts[match.group(1).strip()] = match.group(2).strip()
    return {key: value for key, value in facts.items() if key and value}
