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
                    "page_messages",
                    "dialogs",
                    "url_changed",
                    "field_id",
                    "file_name",
                    "local_path",
                    "profile_path",
                    "has_value",
                    "requirement",
                    "page_url",
                    "page_title",
                    "field_label",
                    "page_hint",
                    "upload_log",
                )
                if key in result
            }
            if "fields" in result and isinstance(result["fields"], list):
                summary["field_count"] = len(result["fields"])
            if "controls" in result and isinstance(result["controls"], list):
                summary["control_count"] = len(result["controls"])
        self.event("tool", step=step, name=name, arguments=arguments, result=summary)
        if name in {"upload_from_profile", "upload_local_file"} and isinstance(result, dict):
            self.upload(step=step, tool=name, result=result, arguments=arguments)

    def upload(
        self,
        *,
        step: int,
        tool: str,
        result: dict[str, Any],
        arguments: dict[str, Any] | None = None,
    ) -> None:
        """Dedicated upload trajectory: local file ↔ webpage field/requirement."""
        args = arguments or {}
        page = {
            "url": result.get("page_url") or result.get("url"),
            "title": result.get("page_title"),
            "field_id": result.get("field_id") or args.get("field_id"),
            "field_label": result.get("field_label") or result.get("label"),
            "field_name": result.get("field_name"),
            "field_accept": result.get("field_accept"),
            "page_hint": result.get("page_hint"),
            "requirement": result.get("requirement") or args.get("requirement"),
        }
        file_info = {
            "local_path": result.get("local_path") or args.get("path"),
            "file_name": result.get("file_name"),
            "profile_path": result.get("profile_path") or args.get("profile_path"),
        }
        self.event(
            "upload",
            step=step,
            tool=tool,
            ok=bool(result.get("ok")),
            error=result.get("error"),
            file=file_info,
            page=page,
            has_value=result.get("has_value"),
        )
        line = (
            f"[upload] ok={bool(result.get('ok'))} "
            f"file={file_info.get('file_name') or file_info.get('local_path') or '-'} "
            f"← page_label={page.get('field_label') or '-'} "
            f"requirement={page.get('requirement') or '-'} "
            f"url={page.get('url') or '-'}"
        )
        # Always surface upload audits on console (even when also_print=False).
        print(line, flush=True)
        self.event("trace", message=line)
