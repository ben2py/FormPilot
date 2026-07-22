from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any


SCAN_SCRIPT = r"""
() => {
  let sequence = Number(document.documentElement.dataset.formpilotSequence || "0");
  const compact = (v, n = 120) => String(v || "").replace(/\s+/g, " ").trim().slice(0, n);
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0" && el.getClientRects().length > 0;
  };
  const idFor = (el, prefix) => {
    if (!el.dataset.formpilotId) {
      sequence += 1;
      el.dataset.formpilotId = `${prefix}-${sequence}`;
    }
    return el.dataset.formpilotId;
  };
  const cleanLabelText = (label) => {
    if (!label) return "";
    const clone = label.cloneNode(true);
    clone.querySelectorAll("input, select, textarea, button, [contenteditable='true']").forEach(x => x.remove());
    return compact(clone.innerText || clone.textContent);
  };
  const explicitLabel = (el) => {
    if (!el.id) return "";
    const label = Array.from(document.querySelectorAll("label[for]")).find(x => x.getAttribute("for") === el.id);
    return cleanLabelText(label);
  };
  const nearby = (el) => {
    const wrapping = el.closest("label");
    if (wrapping) return cleanLabelText(wrapping);
    const siblings = [el.previousElementSibling, el.nextElementSibling].map(x => compact(x && x.innerText, 70)).filter(Boolean);
    if (siblings.length) return siblings.join(" ");
    let parent = el.parentElement;
    for (let i = 0; parent && i < 4; i += 1, parent = parent.parentElement) {
      const text = compact(parent.innerText, 160);
      if (text && text.length <= 120) return text;
    }
    return "";
  };
  const labelFor = (el) => explicitLabel(el) || compact(el.getAttribute("aria-label")) || nearby(el)
    || compact(el.getAttribute("placeholder")) || compact(el.name) || compact(el.id) || "未命名字段";

  const fields = Array.from(document.querySelectorAll("input, select, textarea, [contenteditable='true']"))
    .filter(visible).slice(0, 300).map(el => {
      const tag = el.tagName.toLowerCase();
      const type = tag === "select" ? "select" : String(el.getAttribute("type") || tag).toLowerCase();
      return {
        field_id: idFor(el, "field"), tag, type, id: el.id || "", name: el.name || "",
        label: labelFor(el), placeholder: el.getAttribute("placeholder") || "",
        required: Boolean(el.required || el.getAttribute("aria-required") === "true"),
        disabled: Boolean(el.disabled), read_only: Boolean(el.readOnly),
        current_value: type === "checkbox" || type === "radio" ? Boolean(el.checked) : String(el.value || ""),
        option_value: type === "radio" || type === "checkbox" ? String(el.value || "") : null,
        options: tag === "select" ? Array.from(el.options).slice(0, 200).map(o => ({text: compact(o.text), value: o.value, selected: o.selected, disabled: o.disabled})) : null
      };
    });
  const controls = Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit'], a[role='button']"))
    .filter(visible).slice(0, 100).map(el => ({
      control_id: idFor(el, "control"),
      label: compact(el.innerText || el.value || el.getAttribute("aria-label") || el.title || "未命名按钮"),
      type: String(el.getAttribute("type") || el.tagName).toLowerCase(),
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true")
    }));
  document.documentElement.dataset.formpilotSequence = String(sequence);
  const feedback = Array.from(document.querySelectorAll("[role='alert'], .error, .invalid-feedback, .el-form-item__error, .ant-form-item-explain-error"))
    .filter(visible).slice(0, 30).map(el => compact(el.innerText, 200)).filter(Boolean);
  return {title: document.title, url: location.href, fields, controls, feedback};
}
"""


class PlaywrightFormBrowser:
    def __init__(self, *, headless: bool = False, profile_dir: str | Path = ".formpilot/browser-profile", cdp_url: str | None = None) -> None:
        self.headless = headless
        self.profile_dir = Path(profile_dir)
        self.cdp_url = cdp_url
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.page: Any = None
        self.last_snapshot: dict[str, Any] | None = None

    async def start(self, url: str) -> None:
        local_browsers = Path(".formpilot/browsers")
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and local_browsers.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers.resolve())
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，请运行: pip install -e . && playwright install chromium") from exc

        self._playwright = await async_playwright().start()
        if self.cdp_url:
            self._browser = await self._playwright.chromium.connect_over_cdp(self.cdp_url)
            self._context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir.resolve()),
                headless=self.headless,
                viewport={"width": 1440, "height": 960},
            )
        self.page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self.page.goto(url, wait_until="domcontentloaded")

    async def close(self) -> None:
        if self._context and not self.cdp_url:
            await self._context.close()
        if self._browser and self.cdp_url:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def inspect(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Browser is not started")
        snapshot = await self.page.evaluate(SCAN_SCRIPT)
        self.last_snapshot = snapshot
        return snapshot

    async def wait_and_rescan(self, milliseconds: int = 800) -> dict[str, Any]:
        await asyncio.sleep(max(0, min(milliseconds, 5000)) / 1000)
        return await self.inspect()

    async def _field(self, field_id: str) -> tuple[Any, dict[str, Any]]:
        snapshot = await self.inspect()
        metadata = next((field for field in snapshot["fields"] if field["field_id"] == field_id), None)
        if metadata is None:
            raise KeyError(f"Field not found or no longer visible: {field_id}")
        locator = self.page.locator(f'[data-formpilot-id="{field_id}"]')
        if await locator.count() != 1:
            raise RuntimeError(f"Field locator is not unique: {field_id}")
        return locator, metadata

    async def fill(self, field_id: str, value: Any) -> dict[str, Any]:
        locator, field = await self._field(field_id)
        if field["disabled"] or field["read_only"]:
            return {"ok": False, "error": "字段不可编辑", "field": field}
        text_value = str(value)
        if field["tag"] == "select":
            options = field.get("options") or []
            normalized = lambda text: re.sub(r"[\s_\-—:：]", "", str(text)).lower()
            wanted = normalized(text_value)
            option = next((o for o in options if normalized(o["text"]) == wanted or normalized(o["value"]) == wanted), None)
            if option is None:
                option = next((o for o in options if wanted and (wanted in normalized(o["text"]) or normalized(o["text"]) in wanted)), None)
            if option is None:
                return {"ok": False, "error": "当前下拉选项没有资料对应值", "available_options": [o["text"] for o in options]}
            await locator.select_option(value=option["value"])
            expected: Any = option["value"]
        elif field["type"] in {"checkbox", "radio"}:
            await locator.set_checked(bool(value))
            expected = bool(value)
        else:
            await locator.fill(text_value)
            expected = text_value
        verified = await self.verify(field_id, expected)
        return {"ok": verified["matches"], "field_id": field_id, "verification": verified}

    async def verify(self, field_id: str, expected: Any | None = None) -> dict[str, Any]:
        locator, field = await self._field(field_id)
        if field["type"] in {"checkbox", "radio"}:
            actual: Any = await locator.is_checked()
        else:
            actual = await locator.input_value()
        return {
            "ok": True,
            "field_id": field_id,
            "actual": actual,
            "matches": expected is None or actual == expected,
        }

    async def click(self, control_id: str) -> dict[str, Any]:
        snapshot = await self.inspect()
        control = next((item for item in snapshot["controls"] if item["control_id"] == control_id), None)
        if control is None:
            raise KeyError(f"Control not found or no longer visible: {control_id}")
        locator = self.page.locator(f'[data-formpilot-id="{control_id}"]')
        if await locator.count() != 1:
            raise RuntimeError(f"Control locator is not unique: {control_id}")
        old_url = self.page.url
        await locator.click()
        await self.page.wait_for_timeout(250)
        return {"ok": True, "label": control["label"], "old_url": old_url, "new_url": self.page.url}
