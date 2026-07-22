from __future__ import annotations

import asyncio
import os
import re
from datetime import date
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
    clone.querySelectorAll("input, select, textarea, button, [contenteditable='true'], [role='listbox'], [role='tree'], [role='dialog']").forEach(x => x.remove());
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


WIDGET_SCAN_SCRIPT = r"""
() => {
  let sequence = Number(document.documentElement.dataset.formpilotSequence || "0");
  const compact = (v, n = 240) => String(v || "").replace(/\s+/g, " ").trim().slice(0, n);
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
  const rootSelector = [
    "[role='listbox']", "[role='dialog']", "[role='tree']", "[role='grid']",
    ".el-popper", ".el-picker-panel", ".el-cascader-panel",
    ".ant-select-dropdown", ".ant-cascader-menus", ".ant-picker-dropdown",
    "[class*='cascader-panel']", "[class*='picker-panel']", "[class*='picker-dropdown']",
    "[class*='date-panel']", "[class*='calendar-panel']", "[class*='calendar-popover']"
  ].join(",");
  const roots = Array.from(new Set(Array.from(document.querySelectorAll(rootSelector)).filter(visible))).slice(0, 30);
  const rootFor = (el) => roots.findLast ? roots.findLast(root => root.contains(el)) : [...roots].reverse().find(root => root.contains(el));
  const optionSelector = [
    "[role='option']", "[role='treeitem']", "[role='gridcell']",
    "li", "td", "[class*='cascader-node']", "[class*='select-option']",
    "[class*='menu-item']", "[class*='date-table-cell']", "[class*='calendar-cell']"
  ].join(",");
  const optionElements = Array.from(new Set(roots.flatMap(root => Array.from(root.querySelectorAll(optionSelector)))))
    .filter(visible).slice(0, 500);
  const options = optionElements.map(el => {
    const root = rootFor(el);
    const groups = root ? Array.from(root.querySelectorAll("ul, [role='menu'], [role='group'], .ant-cascader-menu, .el-scrollbar__view")) : [];
    const group = groups.find(candidate => candidate.contains(el));
    const className = typeof el.className === "string" ? el.className : "";
    return {
      option_id: idFor(el, "option"),
      widget_id: root ? idFor(root, "widget") : null,
      text: compact(el.innerText || el.textContent, 120),
      value: el.getAttribute("data-value") || el.getAttribute("data-date") || el.getAttribute("value") || "",
      title: el.getAttribute("title") || "",
      aria_label: el.getAttribute("aria-label") || "",
      role: el.getAttribute("role") || el.tagName.toLowerCase(),
      class_name: compact(className, 180),
      level: group ? groups.indexOf(group) : null,
      selected: el.getAttribute("aria-selected") === "true" || /selected|active|checked/.test(className),
      disabled: el.getAttribute("aria-disabled") === "true" || /disabled/.test(className)
    };
  });
  const controlElements = Array.from(new Set(roots.flatMap(root => Array.from(root.querySelectorAll("button, [role='button'], a")))))
    .filter(visible).slice(0, 300);
  const controls = controlElements.map(el => {
    const root = rootFor(el);
    const className = typeof el.className === "string" ? el.className : "";
    return {
      widget_control_id: idFor(el, "widget-control"),
      widget_id: root ? idFor(root, "widget") : null,
      label: compact(el.innerText || el.getAttribute("aria-label") || el.title || el.textContent, 120),
      title: el.getAttribute("title") || "",
      aria_label: el.getAttribute("aria-label") || "",
      class_name: compact(className, 180),
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true")
    };
  });
  const widgets = roots.map(root => {
    const className = typeof root.className === "string" ? root.className : "";
    const header = root.querySelector(".el-date-picker__header, .ant-picker-header, [class*='calendar-header'], [class*='picker-header'], [data-current-year]");
    const headerText = compact(header && (header.innerText || header.textContent), 160);
    const combined = `${root.getAttribute("data-current-year") || ""} ${root.getAttribute("data-current-month") || ""} ${headerText}`;
    const yearMatch = combined.match(/(?:19|20)\d{2}/);
    const monthMatch = combined.match(/(?:^|[^\d])(?:19|20)\d{2}[^\d]{0,3}(1[0-2]|0?[1-9])(?:月|[^\d]|$)/) || combined.match(/(?:^|[^\d])(1[0-2]|0?[1-9])月/);
    return {
      widget_id: idFor(root, "widget"),
      role: root.getAttribute("role") || "",
      class_name: compact(className, 180),
      summary: compact(root.innerText || root.textContent, 500),
      header_text: headerText,
      current_year: yearMatch ? Number(yearMatch[0]) : null,
      current_month: monthMatch ? Number(monthMatch[1]) : null
    };
  });
  document.documentElement.dataset.formpilotSequence = String(sequence);
  return {widgets, options, controls};
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

    async def inspect_widget(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Browser is not started")
        return await self.page.evaluate(WIDGET_SCAN_SCRIPT)

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

    async def open_field(self, field_id: str) -> dict[str, Any]:
        locator, field = await self._field(field_id)
        if field["disabled"]:
            return {"ok": False, "error": "字段不可交互"}
        await locator.click()
        await self.page.wait_for_timeout(150)
        widget = await self.inspect_widget()
        return {
            "ok": True,
            "field_id": field_id,
            "widgets": widget["widgets"],
            "options": widget["options"][:200],
            "controls": widget["controls"][:100],
        }

    async def _click_widget_item(self, item_id: str) -> None:
        locator = self.page.locator(f'[data-formpilot-id="{item_id}"]')
        if await locator.count() != 1:
            raise RuntimeError(f"Widget item is missing or ambiguous: {item_id}")
        await locator.click()
        await self.page.wait_for_timeout(150)

    async def click_widget_option(self, option_id: str) -> dict[str, Any]:
        widget = await self.inspect_widget()
        option = next((item for item in widget["options"] if item["option_id"] == option_id), None)
        if option is None:
            return {"ok": False, "error": "选项已消失，请重新 inspect_widget"}
        if option["disabled"]:
            return {"ok": False, "error": "选项不可用"}
        await self._click_widget_item(option_id)
        return {"ok": True, "option_id": option_id, "selected": True}

    async def click_widget_control(self, widget_control_id: str) -> dict[str, Any]:
        widget = await self.inspect_widget()
        control = next((item for item in widget["controls"] if item["widget_control_id"] == widget_control_id), None)
        if control is None:
            return {"ok": False, "error": "弹层控件已消失，请重新 inspect_widget"}
        if control["disabled"]:
            return {"ok": False, "error": "弹层控件不可用"}
        await self._click_widget_item(widget_control_id)
        return {"ok": True, "widget_control_id": widget_control_id, "clicked": True}

    @staticmethod
    def _normalized(value: Any) -> str:
        return re.sub(r"[\s_\-—:：省市区县]", "", str(value)).lower()

    async def select_cascade(self, field_id: str, path: list[Any]) -> dict[str, Any]:
        if not path:
            return {"ok": False, "error": "级联路径为空"}
        opened = await self.open_field(field_id)
        if not opened["ok"]:
            return opened
        selected_count = 0
        for level, raw_value in enumerate(path):
            wanted = self._normalized(raw_value)
            widget = await self.inspect_widget()
            candidates = [
                item for item in widget["options"]
                if not item["disabled"] and self._normalized(item["text"] or item["value"]) == wanted
            ]
            level_candidates = [item for item in candidates if item.get("level") in {None, level}]
            if level_candidates:
                candidates = level_candidates
            if len(candidates) != 1:
                return {
                    "ok": False,
                    "error": "级联选项不存在或不唯一",
                    "level": level,
                    "candidate_count": len(candidates),
                    "available_options": [item["text"] for item in widget["options"] if not item["disabled"]][:100],
                }
            await self._click_widget_item(candidates[0]["option_id"])
            selected_count += 1
        locator, _field = await self._field(field_id)
        actual = await locator.input_value()
        return {"ok": selected_count == len(path), "field_id": field_id, "selected_levels": selected_count, "has_value": bool(actual)}

    @staticmethod
    def _control_direction(control: dict[str, Any]) -> str | None:
        text = " ".join(str(control.get(key, "")) for key in ("label", "title", "aria_label", "class_name")).lower()
        if any(token in text for token in ("上一年", "前一年", "previous year", "prev year", "super-prev", "d-arrow-left")) or control.get("label") == "<<":
            return "prev_year"
        if any(token in text for token in ("下一年", "后一年", "next year", "super-next", "d-arrow-right")) or control.get("label") == ">>":
            return "next_year"
        if any(token in text for token in ("上个月", "上一月", "previous month", "prev month", "arrow-left")) or control.get("label") == "<":
            return "prev_month"
        if any(token in text for token in ("下个月", "下一月", "next month", "arrow-right")) or control.get("label") == ">":
            return "next_month"
        return None

    async def _calendar_state(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        widget = await self.inspect_widget()
        calendar = next((item for item in widget["widgets"] if re.search(r"date|calendar|picker", item["class_name"], re.I)), None)
        if calendar is None and len(widget["widgets"]) == 1:
            calendar = widget["widgets"][0]
        return widget, calendar

    async def set_date(self, field_id: str, value: Any) -> dict[str, Any]:
        target = date.fromisoformat(str(value))
        locator, field = await self._field(field_id)
        if field["type"] == "date" and not field["read_only"]:
            await locator.fill(target.isoformat())
            verified = await self.verify(field_id, target.isoformat())
            return {"ok": verified["matches"], "field_id": field_id, "mode": "native"}

        opened = await self.open_field(field_id)
        if not opened["ok"]:
            return opened
        for _ in range(240):
            widget, calendar = await self._calendar_state()
            if calendar is None:
                return {"ok": False, "error": "打开字段后未识别到日期面板"}

            exact_dates = [
                item for item in widget["options"]
                if target.isoformat() in " ".join(str(item.get(key, "")) for key in ("value", "title", "aria_label"))
                and not item["disabled"]
            ]
            if len(exact_dates) == 1:
                await self._click_widget_item(exact_dates[0]["option_id"])
                actual = await locator.input_value()
                return {"ok": bool(actual), "field_id": field_id, "mode": "calendar", "has_value": bool(actual)}

            current_year = calendar.get("current_year")
            current_month = calendar.get("current_month")
            controls = [item for item in widget["controls"] if item.get("widget_id") == calendar.get("widget_id") and not item["disabled"]]
            by_direction = {direction: item for item in controls if (direction := self._control_direction(item))}
            direction: str | None = None
            if current_year is not None and current_year != target.year:
                direction = "prev_year" if current_year > target.year else "next_year"
                if direction not in by_direction:
                    direction = "prev_month" if current_year > target.year else "next_month"
            elif current_month is not None and current_month != target.month:
                forward = (target.month - current_month) % 12
                direction = "next_month" if forward <= 6 else "prev_month"

            if direction and direction in by_direction:
                await self._click_widget_item(by_direction[direction]["widget_control_id"])
                continue

            day_candidates = [
                item for item in widget["options"]
                if item["text"].strip() == str(target.day)
                and not item["disabled"]
                and not re.search(r"prev|next|outside|other-month", item["class_name"], re.I)
            ]
            if current_year == target.year and (current_month in {None, target.month}) and len(day_candidates) == 1:
                await self._click_widget_item(day_candidates[0]["option_id"])
                actual = await locator.input_value()
                return {"ok": bool(actual), "field_id": field_id, "mode": "calendar", "has_value": bool(actual)}
            return {
                "ok": False,
                "error": "无法确定日期面板的下一步操作",
                "calendar": {"header_text": calendar.get("header_text"), "current_year": current_year, "current_month": current_month},
                "available_controls": [{"label": item["label"], "title": item["title"]} for item in controls],
            }
        return {"ok": False, "error": "日期导航超过 240 步，已安全停止"}

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
