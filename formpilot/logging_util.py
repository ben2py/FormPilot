from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


class RunLogger:
    """Console + JSONL action trajectory logger for one FormPilot run."""

    def __init__(self, log_dir: str | Path = ".formpilot/logs", *, also_print: bool = True) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = self.log_dir / f"run-{stamp}.jsonl"
        self.also_print = also_print
        self._fh: TextIO = self.path.open("a", encoding="utf-8")
        self.event("run_started", path=str(self.path))

    def close(self) -> None:
        if not self._fh.closed:
            self.event("run_finished")
            self._fh.close()

    def __call__(self, message: str) -> None:
        """TraceHandler-compatible console sink that also persists the line."""
        if self.also_print:
            print(message)
        self.event("trace", message=message)

    def event(self, event_type: str, **payload: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            **payload,
        }
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()

    def tool(self, *, step: int, name: str, arguments: dict[str, Any], result: Any) -> None:
        summary = result
        if isinstance(result, dict):
            summary = {
                key: result[key]
                for key in (
                    "ok",
                    "blocked",
                    "confirmation_required",
                    "error",
                    "url",
                    "label",
                    "message",
                    "user_guidance",
                    "matches",
                    "truncated",
                    "path",
                    "filled",
                    "errors",
                    "captcha_backend",
                    "captcha_ocr_length",
                    "captcha_attempts",
                    "needs_human",
                    "clicked_login",
                    "selected_project",
                    "incomplete_required",
                    "incomplete_required_count",
                    "updated_paths",
                    "hint",
                    "dismissed",
                )
                if key in result
            }
            if "fields" in result and isinstance(result["fields"], list):
                summary["field_count"] = len(result["fields"])
            if "controls" in result and isinstance(result["controls"], list):
                summary["control_count"] = len(result["controls"])
        self.event("tool", step=step, name=name, arguments=arguments, result=summary)
