from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .credentials import match_select_option


SESSION_ERROR_PATTERN = re.compile(
    r"程序开了小差|异常信息|关闭谷歌浏览器后重新打开|请尝试以下方法进行恢复",
    re.I,
)


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
  const normalizeLabel = (text) => compact(String(text || "").replace(/[*＊]+/g, " ").replace(/[：:]+$/g, " "));
  // Chinese forms almost always mark required fields with * / ＊ next to the label.
  const textLooksRequired = (text) => /[*＊]|必填|必需|必须/.test(String(text || ""));
  const cellLooksRequired = (cell) => {
    if (!cell) return false;
    const raw = String(cell.innerText || cell.textContent || "");
    if (textLooksRequired(raw)) return true;
    return Boolean(cell.querySelector(
      ".red, .required, .require, .must, font[color*='red' i], [style*='color:red' i], [style*='color: red' i], [class*='required'], [class*='asterisk']"
    ));
  };
  const rawTableLabel = (el) => {
    const td = el.closest("td, th");
    if (!td) return "";
    const prev = td.previousElementSibling;
    if (prev && (prev.tagName === "TD" || prev.tagName === "TH")) {
      const text = cleanLabelText(prev) || compact(prev.innerText || prev.textContent);
      // Skip empty or control-only cells; family grids put labels in the header row.
      if (text && text.length <= 40 && !/^(选择|查询|浏览)$/.test(text)) return text;
    }
    const row = td.parentElement;
    if (row) {
      const th = Array.from(row.children).find(x => x.tagName === "TH");
      if (th && th !== td) {
        const text = cleanLabelText(th) || compact(th.innerText || th.textContent);
        if (text) return text;
      }
    }
    // Column headers: 姓名/关系/单位/电话 style tables (family members, etc.).
    const table = td.closest("table");
    if (table && row) {
      const cellIndex = Array.from(row.children).indexOf(td);
      if (cellIndex >= 0) {
        const rows = Array.from(table.querySelectorAll("tr"));
        const headerRow = table.querySelector("thead tr") || rows.find(r => {
          if (r === row || r.querySelector("input, select, textarea")) return false;
          const texts = Array.from(r.children).map(c => compact(c.innerText || c.textContent, 40));
          return texts.some(t => /姓名|关系|单位|职务|电话|手机|称谓/.test(t));
        });
        if (headerRow && headerRow.children[cellIndex]) {
          const headerCell = headerRow.children[cellIndex];
          const text = cleanLabelText(headerCell) || compact(headerCell.innerText || headerCell.textContent, 40);
          if (text) return text;
        }
      }
    }
    return "";
  };
  const tableLabel = (el) => normalizeLabel(rawTableLabel(el));
  const rawExplicitLabel = (el) => {
    if (!el.id) return "";
    const label = Array.from(document.querySelectorAll("label[for]")).find(x => x.getAttribute("for") === el.id);
    return cleanLabelText(label);
  };
  const explicitLabel = (el) => normalizeLabel(rawExplicitLabel(el));
  const rawNearby = (el) => {
    const wrapping = el.closest("label");
    if (wrapping) return cleanLabelText(wrapping);
    const siblings = [el.previousElementSibling, el.nextElementSibling].map(x => compact(x && x.innerText, 70)).filter(Boolean);
    if (siblings.length) return siblings.join(" ");
    return "";
  };
  const nearby = (el) => normalizeLabel(rawNearby(el));
  const labelFor = (el) => tableLabel(el) || explicitLabel(el) || compact(el.getAttribute("aria-label")) || nearby(el)
    || compact(el.getAttribute("placeholder")) || compact(el.name) || compact(el.id) || "未命名字段";
  const isRequired = (el) => {
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    // Prefer visible "*" next to the label — the common web convention.
    if (textLooksRequired(rawTableLabel(el)) || textLooksRequired(rawExplicitLabel(el)) || textLooksRequired(rawNearby(el))) {
      return true;
    }
    const wrapping = el.closest("label");
    if (wrapping && (textLooksRequired(wrapping.innerText || wrapping.textContent) || cellLooksRequired(wrapping))) {
      return true;
    }
    const td = el.closest("td, th");
    if (td && cellLooksRequired(td.previousElementSibling)) return true;
    if (td && cellLooksRequired(td)) return true;
    // Same-row label cells (multi-column: 标签|输入|标签|输入).
    const row = el.closest("tr");
    if (row && td) {
      const cells = Array.from(row.children);
      const idx = cells.indexOf(td);
      if (idx > 0 && cellLooksRequired(cells[idx - 1])) return true;
      const header = cells.find(x => x.tagName === "TH" || x.classList.contains("label"));
      if (cellLooksRequired(header)) return true;
    }
    // Walk a few ancestors for "*姓名" style labels outside strict table markup.
    let parent = el.parentElement;
    for (let i = 0; parent && i < 5; i += 1, parent = parent.parentElement) {
      if (parent.matches && parent.matches("form, body, html, table, tbody")) break;
      const own = compact(
        Array.from(parent.childNodes)
          .filter(n => n.nodeType === 3 || (n.nodeType === 1 && !n.contains(el) && !/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(n.tagName || "")))
          .map(n => n.innerText || n.textContent || "")
          .join(" "),
        80
      );
      if (textLooksRequired(own)) return true;
    }
    const item = el.closest(".el-form-item, .ant-form-item, .form-group, .layui-form-item, .form-item, .field, .item");
    if (item && (textLooksRequired(item.innerText || "") || item.querySelector(".required, .red, [class*='asterisk']"))) return true;
    return false;
  };
  const selectMeaningful = (el) => {
    const opt = el.selectedOptions && el.selectedOptions[0];
    if (!opt) return {value: "", meaningful: false};
    const text = compact(opt.text);
    const value = String(opt.value || "");
    const placeholder = /请选择|^-+$|^\s*$/.test(text) || value === "-1" || value === "";
    return {value, text, meaningful: !placeholder && !opt.disabled};
  };
  const isPlaceholderValue = (v) => {
    const text = compact(v);
    return !text || /^(请选择|点击选择|选择|请填写|\-+请选择\-+)/.test(text) || text === "-" || text === "--" || text === "—";
  };
  const isRegionDetailLabel = (label) => /详细|单位(?!地)|邮编|邮政|编码|电话|手机|邮箱|email/i.test(String(label || ""));
  const looksLikeRegionLabel = (label) => {
    const text = String(label || "");
    if (isRegionDetailLabel(text)) return false;
    return /出生地|籍贯|户口所在地|档案所在地|生源地|所在地区|省市县/.test(text);
  };
  const looksLikeRegionPicker = (el, label) => {
    if (looksLikeRegionLabel(label)) return true;
    const blob = `${el.id || ""} ${el.name || ""} ${el.className || ""} ${el.getAttribute("onclick") || ""}`;
    if (/详细|address|dz|yzbm|postcode|zip/i.test(blob)) return false;
    return /^(csd|csdm|jg|jgm|hkszd|daszd|sysd)(_|$)/i.test(`${el.id || ""}_${el.name || ""}`)
      || /\b(csd|csdm|jgm|hkszd|daszd|sysd|area|region|cascade|picker|distpicker)\b/i.test(blob);
  };
  const cellHasChooseLink = (el) => {
    const scopes = [
      el.closest("td, th, .layui-form-item, .form-group, .el-form-item"),
      el.closest("tr"),
    ].filter(Boolean);
    for (const scope of scopes) {
      const nodes = scope.querySelectorAll("a, button, input[type='button'], span, [onclick]");
      for (const node of nodes) {
        const text = compact(node.innerText || node.value || node.title || "");
        if (/^(选择|查询|浏览|挑选)$/.test(text) || /^选择/.test(text) && text.length <= 6) return true;
      }
    }
    return false;
  };
  const looksLikeCatalogPicker = (el, label) => {
    const text = String(label || "");
    if (/所在学校|毕业院校|学校名称|所在专业|所学专业|专业名称|报考单位|本科院校/.test(text)) return true;
    const blob = `${el.id || ""} ${el.name || ""}`;
    return /bkbydw|bydw|bkbyzy|byzy|yxdm|zydm|xxdm/i.test(blob) || /Show$/i.test(el.id || "");
  };
  const looksLikeMonthField = (el, label) => {
    const text = String(label || "");
    if (/入学年月|毕业年月|预计毕业|入学时间|毕业时间/.test(text) && !/日/.test(text.replace(/年月/g, ""))) return true;
    if (/年月/.test(text)) return true;
    const blob = `${el.id || ""} ${el.name || ""} ${el.getAttribute("onclick") || ""} ${el.className || ""}`;
    return /\b(rxny|byny|byrq|rxrq|Wdate|laydate|yyyy-MM)\b/i.test(blob);
  };

  const fields = Array.from(document.querySelectorAll("input, select, textarea, [contenteditable='true']"))
    .filter(visible).slice(0, 300).map(el => {
      const tag = el.tagName.toLowerCase();
      const type = tag === "select" ? "select" : String(el.getAttribute("type") || tag).toLowerCase();
      const label = labelFor(el);
      let current_value = type === "checkbox" || type === "radio" ? Boolean(el.checked) : String(el.value || "");
      let has_value = type === "checkbox" || type === "radio" ? Boolean(el.checked) : !isPlaceholderValue(current_value);
      if (tag === "select") {
        const selected = selectMeaningful(el);
        current_value = selected.value;
        has_value = selected.meaningful;
      }
      const readOnly = Boolean(el.readOnly);
      const disabled = Boolean(el.disabled);
      // Tongji region/school/major widgets are often disabled/readonly; real opener is nearby「选择」.
      const needsCascade = tag !== "select" && type !== "checkbox" && type !== "radio" && (
        looksLikeRegionPicker(el, label)
        || ((disabled || readOnly) && (looksLikeCatalogPicker(el, label) || cellHasChooseLink(el)))
      );
      const needsMonth = looksLikeMonthField(el, label) && tag !== "select" && type !== "checkbox" && type !== "radio";
      return {
        field_id: idFor(el, "field"), tag, type, id: el.id || "", name: el.name || "",
        label, placeholder: el.getAttribute("placeholder") || "",
        required: isRequired(el),
        disabled, read_only: readOnly,
        needs_cascade: needsCascade,
        needs_month: needsMonth,
        current_value,
        has_value,
        option_value: type === "radio" || type === "checkbox" ? String(el.value || "") : null,
        options: tag === "select" ? Array.from(el.options).slice(0, 200).map(o => ({text: compact(o.text), value: o.value, selected: o.selected, disabled: o.disabled})) : null
      };
    });
  const controls = [];
  const seenControls = new Set();
  const pushControl = (el, type) => {
    const controlId = idFor(el, "control");
    if (seenControls.has(controlId)) return;
    seenControls.add(controlId);
    controls.push({
      control_id: controlId,
      label: compact(el.innerText || el.value || el.getAttribute("aria-label") || el.title || "未命名按钮"),
      type,
      href: el.tagName === "A" ? compact(el.getAttribute("href"), 200) : "",
      disabled: Boolean(el.disabled || el.getAttribute("aria-disabled") === "true")
    });
  };
  Array.from(document.querySelectorAll("button, input[type='button'], input[type='submit'], a[role='button']"))
    .filter(visible)
    .slice(0, 100)
    .forEach(el => pushControl(el, String(el.getAttribute("type") || el.tagName).toLowerCase()));
  Array.from(document.querySelectorAll("a[href], [role='link']"))
    .filter(visible)
    .filter(el => {
      const href = compact(el.getAttribute("href") || el.href || "", 200);
      if (href && /^javascript:/i.test(href)) return false;
      return Boolean(compact(el.innerText || el.getAttribute("aria-label") || el.title || el.textContent, 80));
    })
    .slice(0, 80)
    .forEach(el => pushControl(el, "link"));
  document.documentElement.dataset.formpilotSequence = String(sequence);
  const feedback = Array.from(document.querySelectorAll("[role='alert'], .error, .invalid-feedback, .el-form-item__error, .ant-form-item-explain-error, .layui-layer-content"))
    .filter(visible).slice(0, 30).map(el => compact(el.innerText, 200)).filter(Boolean);
  return {title: document.title, url: location.href, fields, controls: controls.slice(0, 160), feedback};
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
    "[class*='date-panel']", "[class*='calendar-panel']", "[class*='calendar-popover']",
    ".layui-layer", ".layui-layer-content", ".layui-anim", ".layui-tree",
    ".layui-laydate", "[id^='layui-laydate']",
    ".WdateDiv", "#_my97DP", "[class*='Wdate']",
    "[class*='city-picker']", "[class*='area-picker']", "[class*='region-picker']",
    "[class*='distpicker']", ".xm-select-dl", "[class*='cascade']"
  ].join(",");
  const roots = Array.from(new Set(Array.from(document.querySelectorAll(rootSelector)).filter(visible))).slice(0, 30);
  const rootFor = (el) => roots.findLast ? roots.findLast(root => root.contains(el)) : [...roots].reverse().find(root => root.contains(el));
  const optionSelector = [
    "[role='option']", "[role='treeitem']", "[role='gridcell']",
    "li", "td", "a",
    "[class*='cascader-node']", "[class*='select-option']",
    "[class*='menu-item']", "[class*='date-table-cell']", "[class*='calendar-cell']",
    "[class*='city-item']", "[class*='area-item']", "[class*='province']"
  ].join(",");
  const optionElements = Array.from(new Set(roots.flatMap(root => Array.from(root.querySelectorAll(optionSelector)))))
    .filter(visible)
    .filter(el => {
      const text = compact(el.innerText || el.textContent, 80);
      if (!text || text.length > 40) return false;
      // Parent-layer chrome for Tongji area iframe dialogs — not real region options.
      if (/^(确定|清除|关闭|取消|确认|提交)$/.test(text)) return false;
      // Prefer leaf-ish nodes: skip containers that nest many other option candidates.
      const nested = el.querySelectorAll(optionSelector).length;
      return nested <= 2;
    })
    .slice(0, 500);
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

IFRAME_OPTION_SCAN_SCRIPT = r"""
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
  // Prefer real tree leaves (ztree node_name / anchors), not wrapper li/div with same label.
  const preferred = Array.from(document.querySelectorAll(
    ".node_name, a[treenode], [treenode_a], span.node_name, li a, [role='treeitem'], [role='option']"
  )).filter(visible);
  const fallback = Array.from(document.querySelectorAll(
    "a, li, td, span, button, [onclick]"
  )).filter(visible);
  const seen = new Set();
  const options = [];
  const push = (el) => {
    if (seen.has(el)) return;
    const text = compact(el.innerText || el.textContent, 40);
    if (!text || text.length > 20) return;
    if (/^(确定|清除|关闭|取消|确认|提交|请选择|关键字|搜索)$/.test(text)) return;
    if (/关键字|搜索/.test(text) && text.length <= 8) return;
    // Skip wrappers that still contain other candidate leaves with same visible text.
    const nestedLeaves = el.querySelectorAll(".node_name, a, [role='treeitem']");
    if (nestedLeaves.length > 1) return;
    const className = typeof el.className === "string" ? el.className : "";
    if (/treeSearchInput|search/i.test(className) && el.tagName === "INPUT") return;
    seen.add(el);
    options.push({
      option_id: idFor(el, "iframe-option"),
      text,
      value: el.getAttribute("data-value") || el.getAttribute("value") || "",
      title: el.getAttribute("title") || "",
      class_name: compact(className, 120),
      source: "iframe",
      disabled: el.getAttribute("aria-disabled") === "true" || /disabled/.test(className),
    });
  };
  preferred.forEach(push);
  if (options.length < 20) fallback.forEach(push);
  const search = document.querySelector(
    "input.treeSearchInput, input[class*='earch' i], input[placeholder*='关键字'], input[placeholder*='搜索'], #key, #keyword"
  );
  document.documentElement.dataset.formpilotSequence = String(sequence);
  return {
    options: options.slice(0, 400),
    url: location.href,
    title: document.title,
    has_search: Boolean(search),
  };
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
        self.start_url: str | None = None
        self._session_reset_done = False

    @staticmethod
    def recovery_login_url(url: str) -> str:
        """Map Tongji /login/ error page back to a usable /logon entry."""
        parts = urlsplit(str(url or "").strip())
        if not parts.scheme or not parts.netloc:
            return url
        path = parts.path or "/"
        if re.search(r"/login/?$", path, re.I):
            path = re.sub(r"/login/?$", "/logon", path, flags=re.I)
        elif re.search(r"/login/", path, re.I):
            path = re.sub(r"/login/", "/logon", path, count=1, flags=re.I)
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    async def detect_session_corruption(self) -> dict[str, Any]:
        if self.page is None:
            return {"hit": False}
        info = await self.page.evaluate(
            """() => {
              const title = String(document.title || '');
              const body = String((document.body && document.body.innerText) || '').slice(0, 2500);
              const url = location.href;
              const text = `${title}\\n${body}`;
              const hit = /程序开了小差|异常信息|关闭谷歌浏览器后重新打开|请尝试以下方法进行恢复/.test(text)
                || (/\\/login\\/?$/i.test(location.pathname) && /小差|异常|恢复/.test(text));
              return {hit, title, url, snippet: text.replace(/\\s+/g, ' ').trim().slice(0, 180)};
            }"""
        )
        return info or {"hit": False}

    async def _launch_context(self) -> None:
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

    async def reset_corrupted_profile(self, *, reopen_url: str | None = None) -> dict[str, Any]:
        """Delete persistent profile and reopen a clean login page."""
        target = self.recovery_login_url(reopen_url or self.start_url or (self.page.url if self.page else ""))
        if self.cdp_url:
            if self.page is not None:
                await self.page.goto(target, wait_until="domcontentloaded")
            return {
                "ok": True,
                "recovered": True,
                "mode": "cdp_navigate",
                "url": target,
                "message": "CDP 模式下无法删除本地 profile，已跳转到登录页",
            }

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
            self.page = None

        deleted = False
        if self.profile_dir.exists():
            shutil.rmtree(self.profile_dir, ignore_errors=True)
            deleted = not self.profile_dir.exists()
            print(f"[browser] 检测到同济会话异常页，已删除 {self.profile_dir}", flush=True)

        await self._launch_context()
        await self.page.goto(target, wait_until="domcontentloaded")
        return {
            "ok": True,
            "recovered": True,
            "mode": "profile_reset",
            "deleted_profile": deleted,
            "profile_dir": str(self.profile_dir),
            "url": target,
            "message": f"已删除浏览器配置目录并重新打开 {target}",
        }

    async def maybe_recover_session_error(self, *, reopen_url: str | None = None) -> dict[str, Any]:
        info = await self.detect_session_corruption()
        if not info.get("hit"):
            return {"ok": True, "recovered": False, "hit": False}
        if self._session_reset_done:
            return {
                "ok": False,
                "recovered": False,
                "hit": True,
                "error": "仍停在会话异常页（已重置过一次 profile）",
                "title": info.get("title"),
                "url": info.get("url"),
            }
        self._session_reset_done = True
        result = await self.reset_corrupted_profile(reopen_url=reopen_url)
        result["hit"] = True
        result["previous"] = {"title": info.get("title"), "url": info.get("url"), "snippet": info.get("snippet")}
        # Confirm we left the error page.
        again = await self.detect_session_corruption()
        result["still_error"] = bool(again.get("hit"))
        if again.get("hit"):
            result["ok"] = False
            result["error"] = "重置 profile 后仍看到异常页"
        return result

    async def start(self, url: str) -> None:
        local_browsers = Path(".formpilot/browsers")
        if "PLAYWRIGHT_BROWSERS_PATH" not in os.environ and local_browsers.exists():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers.resolve())
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("缺少 Playwright，请运行: pip install -e . && playwright install chromium") from exc

        self.start_url = url
        self._session_reset_done = False
        self._playwright = await async_playwright().start()
        await self._launch_context()
        await self.page.goto(url, wait_until="domcontentloaded")
        recovery = await self.maybe_recover_session_error(reopen_url=url)
        if recovery.get("recovered"):
            print(f"[browser] {recovery.get('message')}", flush=True)

    async def close(self) -> None:
        try:
            if self._context and not self.cdp_url:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._browser and self.cdp_url:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._context = None
        self._browser = None
        self._playwright = None
        self.page = None

    async def inspect(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Browser is not started")
        snapshot = await self.page.evaluate(SCAN_SCRIPT)
        self.last_snapshot = snapshot
        return snapshot

    async def search_visible_text(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Browser is not started")
        needle = str(query or "").strip()
        if not needle:
            return {"ok": False, "error": "query 不能为空", "matches": []}
        result = await self.page.evaluate(
            """(args) => {
              const query = String(args.query || "").trim().toLowerCase();
              const limit = Number(args.limit || 20);
              if (!query) return {matches: []};
              const compact = (v, n = 240) => String(v || "").replace(/\\s+/g, " ").trim().slice(0, n);
              const visible = (el) => {
                const s = getComputedStyle(el);
                return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0" && el.getClientRects().length > 0;
              };
              const nodes = Array.from(document.querySelectorAll("body *"))
                .filter(visible)
                .filter(el => el.children.length === 0 || ["A","BUTTON","LABEL","LI","TD","TH","OPTION","SPAN","P","DIV","H1","H2","H3","H4"].includes(el.tagName));
              const matches = [];
              const seen = new Set();
              for (const el of nodes) {
                const text = compact(el.innerText || el.textContent || el.value || "", 240);
                if (!text || text.length < query.length) continue;
                if (!text.toLowerCase().includes(query)) continue;
                if (seen.has(text)) continue;
                seen.add(text);
                matches.push({
                  text,
                  tag: el.tagName.toLowerCase(),
                  href: el.tagName === "A" ? compact(el.getAttribute("href") || "", 200) : ""
                });
                if (matches.length >= limit) break;
              }
              return {matches};
            }""",
            {"query": needle, "limit": max(1, min(int(limit), 50))},
        )
        return {"ok": True, "query": needle, "matches": result.get("matches") or []}

    async def capture_captcha_image(self, field_id: str) -> dict[str, Any]:
        """Screenshot the captcha image nearest to a captcha input field."""
        if self.page is None:
            raise RuntimeError("Browser is not started")
        locator = self.page.locator(f'[data-formpilot-id="{field_id}"]')
        if await locator.count() != 1:
            return {"ok": False, "error": "captcha field locator is not unique"}

        handle = await locator.element_handle()
        if handle is None:
            return {"ok": False, "error": "captcha field handle missing"}
        image_info = await self.page.evaluate(
            """(el) => {
              const visible = (node) => {
                if (!node) return false;
                const s = getComputedStyle(node);
                return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0" && node.getClientRects().length > 0;
              };
              const mark = (node) => {
                let sequence = Number(document.documentElement.dataset.formpilotSequence || "0");
                if (!node.dataset.formpilotId) {
                  sequence += 1;
                  node.dataset.formpilotId = `captcha-${sequence}`;
                  document.documentElement.dataset.formpilotSequence = String(sequence);
                }
                return node.dataset.formpilotId;
              };
              const boxOf = (node) => {
                const r = node.getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
              };
              const fieldBox = boxOf(el);
              const scoreNode = (node) => {
                if (!visible(node)) return null;
                const tag = node.tagName.toLowerCase();
                if (tag !== "img" && tag !== "canvas") return null;
                const box = boxOf(node);
                if (box.w < 24 || box.h < 12 || box.w > 360 || box.h > 160) return null;
                const src = String(node.getAttribute("src") || node.currentSrc || "");
                const id = String(node.id || "");
                const cls = String(node.className || "");
                const onclick = String(node.getAttribute("onclick") || "");
                const alt = String(node.getAttribute("alt") || "");
                const blob = `${src} ${id} ${cls} ${onclick} ${alt}`.toLowerCase();
                let score = 0;
                if (/captcha|validate|verify|vcode|yzm|checkcode|rand|authcode/.test(blob)) score += 50;
                if (/code/.test(blob)) score += 10;
                if (tag === "img" || tag === "canvas") score += 5;
                // Prefer siblings/nearby horizontally to the right of the input.
                const dx = Math.abs((box.x + box.w / 2) - (fieldBox.x + fieldBox.w / 2));
                const dy = Math.abs((box.y + box.h / 2) - (fieldBox.y + fieldBox.h / 2));
                if (dy < 40) score += 30;
                if (dy < 80) score += 10;
                if (box.x >= fieldBox.x - 8) score += 15;
                score -= Math.min(dx / 20, 20);
                score -= Math.min(dy / 10, 20);
                // Typical captcha aspect ratio is wider than tall.
                if (box.w >= box.h * 1.2) score += 8;
                return {node, score, box, tag, src: src.slice(0, 200)};
              };

              const candidates = [];
              let parent = el.parentElement;
              for (let i = 0; parent && i < 6; i += 1, parent = parent.parentElement) {
                for (const node of parent.querySelectorAll("img, canvas")) {
                  const scored = scoreNode(node);
                  if (scored) candidates.push(scored);
                }
              }
              for (const node of document.querySelectorAll(
                "img[src*='captcha'], img[src*='code'], img[src*='verify'], img[src*='validate'], img[src*='yzm'], img[onclick*='code'], img[onclick*='Captcha'], canvas"
              )) {
                const scored = scoreNode(node);
                if (scored) candidates.push(scored);
              }
              // Deduplicate by formpilot id / object identity.
              const seen = new Set();
              const unique = [];
              for (const item of candidates) {
                const key = item.node;
                if (seen.has(key)) continue;
                seen.add(key);
                unique.push(item);
              }
              unique.sort((a, b) => b.score - a.score);
              if (!unique.length) return null;
              const best = unique[0];
              return {
                image_id: mark(best.node),
                tag: best.tag,
                score: best.score,
                width: Math.round(best.box.w),
                height: Math.round(best.box.h),
                src: best.src,
              };
            }""",
            handle,
        )
        if not image_info or not image_info.get("image_id"):
            return {"ok": False, "error": "未找到验证码图片"}
        image_locator = self.page.locator(f'[data-formpilot-id="{image_info["image_id"]}"]')
        if await image_locator.count() != 1:
            return {"ok": False, "error": "验证码图片定位不唯一"}
        try:
            await image_locator.wait_for(state="visible", timeout=2000)
        except Exception:
            pass

        # Element screenshots of captcha <img> are often blank white (e.g. Tongji
        # /captcha/imageCode). Prefer downloading the image bytes with the page
        # session cookies, then mirror those bytes into the <img> so OCR and the
        # visible/server captcha stay in sync.
        png = b""
        method = "screenshot"
        src = str(image_info.get("src") or "").strip()
        if str(image_info.get("tag") or "").lower() == "img" and src and not src.lower().startswith("data:"):
            absolute = await self.page.evaluate(
                """(raw) => {
                  try { return new URL(raw, location.href).href; } catch (e) { return ""; }
                }""",
                src,
            )
            if absolute:
                parts = urlsplit(absolute)
                query = dict(parse_qsl(parts.query, keep_blank_values=True))
                query["curDate"] = str(int(time.time() * 1000))
                refresh_url = urlunsplit(
                    (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
                )
                try:
                    response = await self.page.request.get(refresh_url)
                    body = await response.body() if response.ok else b""
                    content_type = (response.headers.get("content-type") or "").lower()
                    if body and len(body) >= 80 and (
                        "image/" in content_type
                        or body[:8] == b"\x89PNG\r\n\x1a\n"
                        or body[:2] == b"\xff\xd8"
                        or body[:6] in {b"GIF87a", b"GIF89a"}
                    ):
                        png = body
                        method = "network"
                        if "jpeg" in content_type or "jpg" in content_type:
                            mime = "image/jpeg"
                        elif "gif" in content_type:
                            mime = "image/gif"
                        elif "webp" in content_type:
                            mime = "image/webp"
                        else:
                            mime = "image/png"
                        data_url = f"data:{mime};base64," + base64.b64encode(png).decode("ascii")
                        await self.page.evaluate(
                            """({imageId, dataUrl}) => {
                              const node = document.querySelector(`[data-formpilot-id="${imageId}"]`);
                              if (node && node.tagName === "IMG") {
                                node.src = dataUrl;
                              }
                            }""",
                            {"imageId": image_info["image_id"], "dataUrl": data_url},
                        )
                except Exception:
                    png = b""

        if not png:
            png = await image_locator.screenshot(type="png")
            method = "screenshot"

        return {
            "ok": True,
            "image_id": image_info["image_id"],
            "tag": image_info.get("tag"),
            "score": image_info.get("score"),
            "width": image_info.get("width"),
            "height": image_info.get("height"),
            "src": image_info.get("src"),
            "method": method,
            "png": png,
        }

    async def wait_and_rescan(self, milliseconds: int = 800) -> dict[str, Any]:
        await asyncio.sleep(max(0, min(milliseconds, 5000)) / 1000)
        return await self.inspect()

    async def close_layui_layers(self) -> int:
        if self.page is None:
            return 0
        result = await self.page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll('.layui-layer, .layui-layer-shade'));
              for (const node of nodes) node.remove();
              return nodes.length;
            }"""
        )
        return int(result or 0)

    async def click_visible_text(self, text: str, *, where: str = "auto") -> dict[str, Any]:
        """Click a visible option/label by text. Model-driven primitive for custom pickers."""
        wanted = str(text or "").strip()
        if not wanted:
            return {"ok": False, "error": "text 不能为空"}
        if len(wanted) > 80:
            return {"ok": False, "error": "text 过长"}
        scope = (where or "auto").strip().lower()
        if scope not in {"auto", "page", "iframe"}:
            return {"ok": False, "error": "where 只能是 auto / page / iframe"}

        async def click_in_page() -> dict[str, Any]:
            return await self.page.evaluate(
                """(wantedRaw) => {
                  const norm = (s) => String(s || '').replace(/[\\s_\\-—:：]/g, '').toLowerCase();
                  const wanted = norm(wantedRaw);
                  const visible = (el) => {
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && el.getClientRects().length > 0;
                  };
                  const nodes = Array.from(document.querySelectorAll(
                    "a, button, li, td, span, div, label, [role='option'], [role='treeitem'], [onclick]"
                  )).filter(visible);
                  const scored = [];
                  for (const el of nodes) {
                    const raw = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!raw || raw.length > 40) continue;
                    if (el.querySelectorAll('a, li, td, button').length > 3) continue;
                    const n = norm(raw);
                    let score = -1;
                    if (n === wanted) score = 100;
                    else if (n.startsWith(wanted) && n.length <= wanted.length + 1) score = 90;
                    else if (wanted.startsWith(n) && wanted.length <= n.length + 1) score = 88;
                    else if (wanted.length >= 4 && n.includes(wanted) && (n.length - wanted.length) <= 2) score = 70;
                    else if (n.length >= 4 && wanted.includes(n) && (wanted.length - n.length) <= 2) score = 65;
                    if (score >= 0) scored.push({el, text: raw, score});
                  }
                  scored.sort((a, b) => b.score - a.score || a.text.length - b.text.length);
                  if (!scored.length) return {ok: false, error: 'not_found'};
                  const best = scored[0].score;
                  const top = scored.filter(i => i.score === best);
                  const shortest = Math.min(...top.map(i => i.text.length));
                  const finalists = top.filter(i => i.text.length === shortest);
                  if (finalists.length !== 1) {
                    return {ok: false, error: 'ambiguous', candidates: finalists.map(i => i.text).slice(0, 20)};
                  }
                  finalists[0].el.click();
                  return {ok: true, text: finalists[0].text, where: 'page'};
                }""",
                wanted,
            )

        tried: list[str] = []
        if scope in {"auto", "iframe"}:
            frame = await self._top_layui_iframe()
            if frame is not None:
                tried.append("iframe")
                result = await self._click_text_in_frame(frame, wanted)
                if result.get("ok"):
                    await self.page.wait_for_timeout(200)
                    return {"ok": True, "text": result.get("text"), "where": "iframe", "matched": result.get("text")}
                if scope == "iframe":
                    return {
                        "ok": False,
                        "error": result.get("error") or "iframe 中未找到",
                        "where": "iframe",
                        "available_options": (result.get("available") or [])[:80],
                    }
            elif scope == "iframe":
                return {"ok": False, "error": "当前没有可见的弹层 iframe", "where": "iframe"}

        if scope in {"auto", "page"}:
            tried.append("page")
            result = await click_in_page()
            if result.get("ok"):
                await self.page.wait_for_timeout(200)
                return result
            if scope == "page":
                return {"ok": False, "error": result.get("error") or "页面中未找到", "where": "page", "candidates": result.get("candidates")}

        return {
            "ok": False,
            "error": "未找到唯一匹配的可见文本",
            "wanted": wanted,
            "tried": tried,
        }

    async def confirm_overlay(self) -> dict[str, Any]:
        """Click the primary confirm button on the topmost overlay/dialog."""
        confirmed = await self._confirm_top_layui_layer()
        await self.page.wait_for_timeout(250)
        return {
            "ok": bool(confirmed),
            "confirmed": bool(confirmed),
            "message": "已点击弹层确定" if confirmed else "未找到可点击的确定按钮",
        }

    async def _top_layui_iframe(self) -> Any | None:
        """Return the content frame of the topmost visible layui area-picker iframe."""
        if self.page is None:
            return None
        for _ in range(25):
            handles = await self.page.query_selector_all(
                ".layui-layer.layui-layer-iframe iframe, .layui-layer-iframe iframe, .layui-layer iframe"
            )
            frames: list[Any] = []
            for handle in handles:
                try:
                    box = await handle.bounding_box()
                    if not box or box.get("width", 0) < 20 or box.get("height", 0) < 20:
                        continue
                    frame = await handle.content_frame()
                    if frame is not None:
                        frames.append(frame)
                except Exception:
                    continue
            if frames:
                return frames[-1]
            await self.page.wait_for_timeout(120)
        return None

    async def inspect_widget(self) -> dict[str, Any]:
        if self.page is None:
            raise RuntimeError("Browser is not started")
        widget = await self.page.evaluate(WIDGET_SCAN_SCRIPT)
        options = list(widget.get("options") or [])
        controls = list(widget.get("controls") or [])
        widgets = list(widget.get("widgets") or [])
        frame = await self._top_layui_iframe()
        iframe_meta: dict[str, Any] | None = None
        if frame is not None:
            try:
                iframe_scan = await frame.evaluate(IFRAME_OPTION_SCAN_SCRIPT)
            except Exception:
                iframe_scan = None
            if isinstance(iframe_scan, dict):
                iframe_meta = {
                    "url": iframe_scan.get("url"),
                    "title": iframe_scan.get("title"),
                    "option_count": len(iframe_scan.get("options") or []),
                    "has_search": bool(iframe_scan.get("has_search")),
                }
                for item in iframe_scan.get("options") or []:
                    item = dict(item)
                    item["source"] = "iframe"
                    options.append(item)
                widgets.append(
                    {
                        "widget_id": "iframe-area-picker",
                        "role": "iframe",
                        "class_name": "layui-layer-iframe",
                        "summary": f"iframe options={iframe_meta['option_count']}",
                        "header_text": str(iframe_scan.get("title") or ""),
                        "current_year": None,
                        "current_month": None,
                    }
                )
        return {
            "widgets": widgets,
            "options": options,
            "controls": controls,
            "iframe": iframe_meta,
        }

    async def _search_in_frame(self, frame: Any, query: str) -> dict[str, Any]:
        """Use the area-tree keyword box when present (Tongji layui iframe)."""
        return await frame.evaluate(
            """(q) => {
              const input = document.querySelector(
                "input.treeSearchInput, input[class*='earch' i], input[placeholder*='关键字'], input[placeholder*='搜索'], #key, #keyword, input[type='text']"
              );
              if (!input || !input.getClientRects().length) return {ok: false, reason: 'no_search'};
              input.focus();
              input.value = '';
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.value = String(q || '');
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
              const parent = input.parentElement || document.body;
              const btn = Array.from(parent.querySelectorAll('a, button, input[type="button"], span, div'))
                .find(node => /搜索|查询|search/i.test(`${node.innerText || ''} ${node.value || ''} ${node.className || ''}`));
              if (btn) {
                btn.click();
                return {ok: true, via: 'button'};
              }
              input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
              input.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', bubbles: true}));
              if (typeof input.form?.requestSubmit === 'function') {
                try { input.form.requestSubmit(); } catch (e) {}
              }
              return {ok: true, via: 'enter'};
            }""",
            str(query),
        )

    async def _click_text_in_frame(self, frame: Any, raw_value: Any) -> dict[str, Any]:
        wanted_raw = str(raw_value or "").strip()
        # Province/city parent filters must NOT go through the keyword box — searching
        # "陕西省" collapses the tree to schools whose names contain 陕西 and then
        # fuzzy-matches the wrong leaf (e.g. 中共陕西省委党校).
        use_search = not bool(
            re.search(r"(省|市|区|县|自治州|地区|盟|旗|特别行政区)$", wanted_raw)
        ) and len(wanted_raw) >= 3
        if use_search:
            try:
                searched = await self._search_in_frame(frame, wanted_raw)
            except Exception:
                searched = {"ok": False}
            if searched.get("ok"):
                await self.page.wait_for_timeout(450)

        return await frame.evaluate(
            """(wantedRaw) => {
              const norm = (s) => String(s || '').replace(/[\\s_\\-—:：]/g, '').toLowerCase();
              const stripAdmin = (s) => String(s || '').replace(/(特别行政区|自治区|省|市|区|县|自治州|地区|盟|旗)$/g, '');
              const wantedFull = norm(wantedRaw);
              const wanted = norm(stripAdmin(wantedRaw)) || wantedFull;
              if (!wanted) return {ok: false, error: 'empty'};
              const visible = (el) => {
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && el.getClientRects().length > 0;
              };
              const depthOf = (el) => {
                let d = 0; let n = el;
                while (n && n !== document.body) { d += 1; n = n.parentElement; }
                return d;
              };
              const preferredSel = ".node_name, a[treenode], [treenode_a], span.node_name, li > a, [role='treeitem'], [role='option'], .province-item, .university-item, .city-item";
              const broadSel = preferredSel + ", a, span, td, button, [onclick]";
              const nodes = Array.from(document.querySelectorAll(broadSel)).filter(visible);
              const scored = [];
              for (const el of nodes) {
                let text = '';
                if (el.matches('.node_name, span.node_name, .province-item, .university-item, .city-item')) {
                  text = String(el.textContent || '').replace(/\\s+/g, ' ').trim();
                } else {
                  const clone = el.cloneNode(true);
                  clone.querySelectorAll('ul, ol, .switch, .button, input, button').forEach(x => x.remove());
                  text = String(clone.innerText || clone.textContent || '').replace(/\\s+/g, ' ').trim();
                }
                if (!text || text.length > 40) continue;
                if (/^(确定|清除|关闭|取消|确认|提交|请选择|关键字|搜索)$/.test(text)) continue;
                const nFull = norm(text);
                const n = norm(stripAdmin(text)) || nFull;
                if (!n) continue;
                let score = -1;
                if (nFull === wantedFull || n === wanted) score = 100;
                else if (nFull === wanted || n === wantedFull) score = 98;
                else if (n.startsWith(wanted) && n.length <= wanted.length + 1) score = 90;
                else if (wanted.startsWith(n) && wanted.length <= n.length + 1) score = 88;
                else if (wanted.length >= 3 && (n.endsWith(wanted) || nFull.endsWith(wantedFull))) score = 82;
                // Tight contains: avoid 陕西 → 中共陕西省委党校, but allow coded majors.
                else if (wanted.length >= 4 && n.includes(wanted) && (n.length - wanted.length) <= 10) score = 72;
                else if (n.length >= 4 && wanted.includes(n) && (wanted.length - n.length) <= 2) score = 65;
                if (score < 0) continue;
                if (el.matches('.province-item, .university-item, .city-item, .node_name, span.node_name, a[treenode], [treenode_a], li > a')) score += 25;
                if (el.tagName === 'A') score += 8;
                const childCandidates = el.querySelectorAll('.node_name, a, [role="treeitem"]').length;
                if (childCandidates > 1) score -= 40;
                scored.push({
                  el,
                  text,
                  score,
                  depth: depthOf(el),
                  childCount: childCandidates,
                  area: (() => { const r = el.getBoundingClientRect(); return r.width * r.height; })(),
                });
              }
              scored.sort((a, b) =>
                b.score - a.score
                || a.childCount - b.childCount
                || b.depth - a.depth
                || a.area - b.area
                || a.text.length - b.text.length
              );
              if (!scored.length) {
                return {
                  ok: false,
                  error: 'not_found',
                  available: nodes.map(n => String(n.innerText || n.textContent || '').replace(/\\s+/g,' ').trim())
                    .filter(t => t && t.length <= 40 && !/^(确定|清除|关闭|取消|关键字|搜索)$/.test(t))
                    .slice(0, 80),
                };
              }
              const bestScore = scored[0].score;
              let finalists = scored.filter(item => item.score === bestScore);
              if (finalists.length > 1) {
                const same = finalists.every(item => norm(item.text) === norm(finalists[0].text));
                if (same) {
                  finalists = [finalists.sort((a, b) => a.childCount - b.childCount || b.depth - a.depth || a.area - b.area)[0]];
                }
              }
              // Prefer shortest exact-ish label when still tied (省 vs 含省名的院校).
              if (finalists.length > 1) {
                const shortest = Math.min(...finalists.map(i => i.text.length));
                finalists = finalists.filter(i => i.text.length === shortest);
              }
              if (finalists.length !== 1) {
                return {
                  ok: false,
                  error: 'ambiguous',
                  candidate_count: finalists.length,
                  available: finalists.map(i => i.text),
                };
              }
              const target = finalists[0].el;
              const clickable = target.closest('a') || target.querySelector('a, .node_name') || target;
              clickable.click();
              return {ok: true, text: finalists[0].text, via: 'leaf'};
            }""",
            wanted_raw,
        )

    async def _confirm_top_layui_layer(self) -> bool:
        if self.page is None:
            return False
        return bool(
            await self.page.evaluate(
                """() => {
                  const visible = (el) => {
                    const s = getComputedStyle(el);
                    return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0' && el.getClientRects().length > 0;
                  };
                  const layers = Array.from(document.querySelectorAll('.layui-layer')).filter(visible);
                  if (!layers.length) return false;
                  const top = layers[layers.length - 1];
                  const btn = top.querySelector('.layui-layer-btn0')
                    || Array.from(top.querySelectorAll('a, button')).find(node => /^(确定|确认|OK)$/i.test(String(node.innerText || '').trim()));
                  if (!btn) return false;
                  btn.click();
                  return true;
                }"""
            )
        )

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
        if field.get("needs_cascade"):
            return {
                "ok": False,
                "error": (
                    "该字段是弹层选择器（地区/学校/专业等）：请用 select_cascade_from_profile，"
                    "或 open_field → inspect_widget → click_visible_text → confirm_overlay"
                ),
                "field_id": field_id,
                "needs_cascade": True,
            }
        if field.get("needs_month") or (
            (field.get("disabled") or field.get("read_only"))
            and re.search(r"入学年月|毕业年月|预计毕业|年月", str(field.get("label") or ""))
        ):
            return {
                "ok": False,
                "error": "该字段是年月选择器：请用 set_date_from_profile（支持 yyyy-MM）",
                "field_id": field_id,
                "needs_month": True,
            }
        if field["disabled"] or field["read_only"]:
            return {"ok": False, "error": "字段不可编辑", "field": field}
        text_value = str(value)
        if field["tag"] == "select":
            options = field.get("options") or []
            option = match_select_option(options, text_value)
            if option is None:
                return {"ok": False, "error": "当前下拉选项没有资料对应值", "available_options": [o["text"] for o in options]}
            await locator.select_option(value=option["value"])
            # Native value may update while layui/custom UI still shows 请选择 — sync events/UI.
            await locator.evaluate(
                """(el, selectedText) => {
                  el.dispatchEvent(new Event('input', {bubbles: true}));
                  el.dispatchEvent(new Event('change', {bubbles: true}));
                  try {
                    if (window.layui && layui.form) layui.form.render('select');
                  } catch (e) {}
                  const wrap = el.closest('.layui-form-select') || el.parentElement;
                  if (wrap) {
                    const title = wrap.querySelector('.layui-select-title input, .layui-select-title, input.layui-input');
                    if (title) {
                      if ('value' in title) title.value = selectedText;
                      else title.textContent = selectedText;
                    }
                  }
                }""",
                str(option.get("text") or text_value),
            )
            await self.page.wait_for_timeout(100)
            expected: Any = option["value"]
            verified = await self.verify(field_id, expected)
            if not verified["matches"]:
                # Fallback: open the visible dropdown UI and click the option text.
                clicked = await locator.evaluate(
                    """(el, wantedText) => {
                      const wrap = el.closest('.layui-form-select') || el.parentElement;
                      const title = wrap && wrap.querySelector('.layui-select-title, .layui-select-title input, input.layui-input');
                      if (title) title.click();
                      else el.click();
                      const nodes = Array.from(document.querySelectorAll('.layui-form-select dl dd, .layui-anim dd, option'));
                      const target = nodes.find(node => {
                        const text = String(node.innerText || node.textContent || '').replace(/\\s+/g, '').trim();
                        const wanted = String(wantedText || '').replace(/\\s+/g, '').trim();
                        return text === wanted || text.includes(wanted) || wanted.includes(text);
                      });
                      if (!target) return false;
                      target.click();
                      return true;
                    }""",
                    str(option.get("text") or text_value),
                )
                if clicked:
                    await self.page.wait_for_timeout(120)
                    verified = await self.verify(field_id, expected)
            return {"ok": verified["matches"], "field_id": field_id, "verification": verified}
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
        pickerish = bool(field.get("needs_cascade")) or bool(
            re.search(
                r"出生地|籍贯|户口所在地|档案所在地|生源地|所在地区|所在学校|所在专业|毕业院校|所学专业",
                str(field.get("label") or ""),
            )
        )
        monthish = bool(field.get("needs_month")) or bool(
            re.search(r"入学年月|毕业年月|预计毕业|年月", str(field.get("label") or ""))
        )
        # Disabled/readonly pickers are opened via nearby triggers, not typed into.
        if field.get("disabled") and not pickerish and not field.get("read_only") and not monthish:
            return {"ok": False, "error": "字段不可交互", "field_id": field_id}
        if pickerish:
            # Avoid stacking multiple area iframes from previous failed attempts.
            closer = getattr(self, "close_layui_layers", None)
            if callable(closer):
                await closer()
            else:
                # Defensive fallback if method is missing on a stale object.
                await self.page.evaluate(
                    """() => {
                      document.querySelectorAll('.layui-layer, .layui-layer-shade').forEach(n => n.remove());
                    }"""
                )
            await self.page.wait_for_timeout(120)
        clicked = await self.page.evaluate(
            """(id) => {
              const el = document.querySelector(`[data-formpilot-id="${id}"]`);
              if (!el) return {ok: false, reason: 'missing'};
              const cell = el.closest('td, th, .layui-form-item, .form-group, .el-form-item') || el.parentElement;
              const row = el.closest('tr');
              const scoreNode = (node) => {
                if (!node || node === el) return -1;
                const raw = String(node.innerText || node.value || node.title || '').replace(/\\s+/g, ' ').trim();
                const text = `${raw} ${node.className || ''} ${node.getAttribute('onclick') || ''} ${node.id || ''}`;
                let score = 0;
                if (/^(选择|查询|浏览|挑选)$/.test(raw)) score += 100;
                if (/选择|请选择|选区|地区|省市/.test(text)) score += 50;
                if (/area|region|city|csd|jg|dq|picker|cascade|Wdate|laydate/i.test(text)) score += 30;
                if (node.tagName === 'A' || node.tagName === 'BUTTON') score += 20;
                if (node.getAttribute('onclick')) score += 15;
                if (node.tagName === 'IMG') score += 10;
                if (/layui-icon|icon|Wdate/.test(String(node.className || ''))) score += 8;
                return score;
              };
              const candidates = [];
              const scopes = [cell, row].filter(Boolean);
              for (const scope of scopes) {
                for (const node of scope.querySelectorAll('a, button, input[type="button"], img, [onclick], span, i, em, div')) {
                  const score = scoreNode(node);
                  if (score > 0) candidates.push({node, score});
                }
              }
              candidates.sort((a, b) => b.score - a.score);
              const tryClick = (node) => {
                try {
                  node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                  node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                  node.click();
                  return true;
                } catch (e) {
                  return false;
                }
              };
              for (const item of candidates) {
                if (tryClick(item.node)) return {ok: true, via: 'trigger', score: item.score};
              }
              // Fall back to the field itself / parent cell even when disabled.
              for (const node of [el, el.parentElement, cell]) {
                if (node && tryClick(node)) return {ok: true, via: 'self'};
              }
              return {ok: false, reason: 'no-trigger'};
            }""",
            field_id,
        )
        if not clicked or not clicked.get("ok"):
            # Last resort: Playwright force-click.
            try:
                await locator.click(force=True, timeout=2000)
            except Exception:
                return {
                    "ok": False,
                    "error": "无法打开字段选择器",
                    "field_id": field_id,
                    "detail": clicked,
                }
        await self.page.wait_for_timeout(550 if pickerish else 400)
        widget = await self.inspect_widget()
        return {
            "ok": True,
            "field_id": field_id,
            "open_via": (clicked or {}).get("via"),
            "widgets": widget["widgets"],
            "options": widget["options"][:200],
            "controls": widget["controls"][:100],
            "iframe": widget.get("iframe"),
        }

    async def _click_widget_item(self, item_id: str) -> None:
        locator = self.page.locator(f'[data-formpilot-id="{item_id}"]')
        if await locator.count() == 1:
            await locator.click(force=True)
            await self.page.wait_for_timeout(250)
            return
        frame = await self._top_layui_iframe()
        if frame is not None:
            frame_locator = frame.locator(f'[data-formpilot-id="{item_id}"]')
            if await frame_locator.count() == 1:
                await frame_locator.click(force=True)
                await self.page.wait_for_timeout(250)
                return
        raise RuntimeError(f"Widget item is missing or ambiguous: {item_id}")

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

    def _match_cascade_option(self, options: list[dict[str, Any]], raw_value: Any, level: int) -> list[dict[str, Any]]:
        wanted = self._normalized(raw_value)
        if not wanted:
            return []
        enabled = [item for item in options if not item.get("disabled")]
        exact = [
            item for item in enabled
            if self._normalized(item.get("text") or item.get("value") or item.get("title")) == wanted
        ]
        level_exact = [item for item in exact if item.get("level") in {None, level}]
        if level_exact:
            return level_exact
        if exact:
            return exact
        soft = [
            item for item in enabled
            if wanted in self._normalized(item.get("text") or item.get("value") or item.get("title"))
            or self._normalized(item.get("text") or item.get("value") or item.get("title")) in wanted
        ]
        level_soft = [item for item in soft if item.get("level") in {None, level}]
        return level_soft or soft

    async def _wait_for_layui_iframe_ready(self, *, min_options: int = 5, timeout_ms: int = 4000) -> Any | None:
        """Wait until the top layui iframe has clickable options (province/school list)."""
        deadline = time.time() + max(0.5, timeout_ms / 1000)
        last_frame = None
        while time.time() < deadline:
            frame = await self._top_layui_iframe()
            if frame is not None:
                last_frame = frame
                try:
                    count = await frame.evaluate(
                        """() => document.querySelectorAll(
                          '.province-item, .university-item, .city-item, .node_name, a[treenode], [role="treeitem"], a, li'
                        ).length"""
                    )
                except Exception:
                    count = 0
                if int(count or 0) >= min_options:
                    return frame
            await self.page.wait_for_timeout(150)
        return last_frame

    async def select_cascade(self, field_id: str, path: list[Any]) -> dict[str, Any]:
        if not path:
            return {"ok": False, "error": "级联路径为空"}
        opened = await self.open_field(field_id)
        if not opened["ok"]:
            return opened

        # Tongji area/school pickers render inside a layui iframe — wait for options to hydrate.
        frame = await self._wait_for_layui_iframe_ready()
        selected_labels: list[str] = []
        if frame is not None:
            for level, raw_value in enumerate(path):
                clicked = await self._click_text_in_frame(frame, raw_value)
                if not clicked.get("ok"):
                    return {
                        "ok": False,
                        "error": "级联选项不存在或不唯一",
                        "mode": "layui_iframe",
                        "level": level,
                        "wanted": str(raw_value),
                        "candidate_count": clicked.get("candidate_count") or 0,
                        "available_options": (clicked.get("available") or [])[:100],
                        "field_id": field_id,
                    }
                selected_labels.append(str(clicked.get("text") or raw_value))
                await self.page.wait_for_timeout(350)
                # Some pickers navigate iframe content; refresh frame handle each level.
                nxt = await self._wait_for_layui_iframe_ready(min_options=1, timeout_ms=2500)
                if nxt is not None:
                    frame = nxt
            confirmed = await self._confirm_top_layui_layer()
            await self.page.wait_for_timeout(350)
            snapshot = await self.inspect()
            field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
            has_value = bool(field and field.get("has_value"))
            if not has_value:
                locator, _field = await self._field(field_id)
                try:
                    actual = await locator.input_value()
                except Exception:
                    actual = ""
                has_value = bool(str(actual or "").strip()) and not re.match(
                    r"^(请选择|点击选择|选择)", str(actual or "").strip()
                )
            return {
                "ok": has_value,
                "field_id": field_id,
                "mode": "layui_iframe",
                "selected_levels": len(selected_labels),
                "selected_labels": selected_labels,
                "confirmed": confirmed,
                "has_value": has_value,
            }

        # Fallback: non-iframe custom widgets on the main page.
        widget = await self.inspect_widget()
        options = [
            item for item in (widget.get("options") or [])
            if not re.fullmatch(r"确定|清除|关闭|取消|确认", str(item.get("text") or "").strip())
        ]
        if not options:
            return {
                "ok": False,
                "error": "已点击地区字段但未出现可选弹层（也未找到 layui iframe）",
                "field_id": field_id,
                "open_via": opened.get("open_via"),
                "widgets": [
                    {"class_name": w.get("class_name"), "summary": w.get("summary")}
                    for w in (widget.get("widgets") or [])[:8]
                ],
            }
        selected_count = 0
        for level, raw_value in enumerate(path):
            widget = await self.inspect_widget()
            page_options = [
                item for item in (widget.get("options") or [])
                if not re.fullmatch(r"确定|清除|关闭|取消|确认", str(item.get("text") or "").strip())
            ]
            candidates = self._match_cascade_option(page_options, raw_value, level)
            if len(candidates) != 1:
                if len(candidates) > 1:
                    candidates = sorted(
                        candidates,
                        key=lambda item: len(str(item.get("text") or item.get("value") or "")),
                    )
                    shortest = len(str(candidates[0].get("text") or candidates[0].get("value") or ""))
                    candidates = [
                        item for item in candidates
                        if len(str(item.get("text") or item.get("value") or "")) == shortest
                    ]
                if len(candidates) != 1:
                    return {
                        "ok": False,
                        "error": "级联选项不存在或不唯一",
                        "mode": "page_widget",
                        "level": level,
                        "wanted": str(raw_value),
                        "candidate_count": len(candidates),
                        "available_options": [item["text"] for item in page_options if not item.get("disabled")][:100],
                    }
            await self._click_widget_item(candidates[0]["option_id"])
            selected_count += 1
            await self.page.wait_for_timeout(200)
        await self._confirm_top_layui_layer()
        snapshot = await self.inspect()
        field = next((item for item in snapshot["fields"] if item["field_id"] == field_id), None)
        has_value = bool(field and field.get("has_value"))
        if not has_value:
            locator, _field = await self._field(field_id)
            try:
                actual = await locator.input_value()
            except Exception:
                actual = ""
            has_value = bool(str(actual or "").strip()) and not re.match(
                r"^(请选择|点击选择|选择)", str(actual or "").strip()
            )
        return {
            "ok": selected_count == len(path) and has_value,
            "field_id": field_id,
            "mode": "page_widget",
            "selected_levels": selected_count,
            "has_value": has_value,
        }

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

    @staticmethod
    def _parse_date_value(value: Any) -> tuple[date, str]:
        """Return (date, display_format) where format is yyyy-MM or yyyy-MM-dd."""
        raw = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}", raw):
            return date.fromisoformat(f"{raw}-01"), "yyyy-MM"
        if re.fullmatch(r"\d{4}/\d{2}", raw):
            year, month = raw.split("/")
            return date(int(year), int(month), 1), "yyyy-MM"
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw):
            parsed = date.fromisoformat(raw)
            return parsed, "yyyy-MM-dd"
        if re.fullmatch(r"\d{4}/\d{1,2}/\d{1,2}", raw):
            parts = [int(p) for p in raw.split("/")]
            return date(parts[0], parts[1], parts[2]), "yyyy-MM-dd"
        return date.fromisoformat(raw), "yyyy-MM-dd"

    async def _set_date_value_js(self, field_id: str, display: str) -> dict[str, Any]:
        """Write a date/month string into readonly WdatePicker/laydate fields."""
        written = await self.page.evaluate(
            """({id, value}) => {
              const el = document.querySelector(`[data-formpilot-id="${id}"]`);
              if (!el) return {ok: false, error: 'missing'};
              const wasReadOnly = el.readOnly;
              const wasDisabled = el.disabled;
              try {
                el.readOnly = false;
                el.disabled = false;
                el.value = value;
                el.setAttribute('value', value);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
                if (typeof el.onchange === 'function') {
                  try { el.onchange(); } catch (e) {}
                }
              } finally {
                el.readOnly = wasReadOnly;
                el.disabled = wasDisabled;
              }
              return {ok: true, value: el.value || ''};
            }""",
            {"id": field_id, "value": display},
        )
        if not written or not written.get("ok"):
            return {"ok": False, "error": "无法写入日期值", "field_id": field_id}
        await self.page.wait_for_timeout(120)
        locator, _field = await self._field(field_id)
        try:
            actual = await locator.input_value()
        except Exception:
            actual = str(written.get("value") or "")
        matches = bool(actual) and (
            actual == display
            or actual.startswith(display)
            or display.startswith(actual)
            or actual.replace("/", "-") == display
        )
        return {
            "ok": matches,
            "field_id": field_id,
            "mode": "direct_value",
            "has_value": bool(actual),
            "display": display,
        }

    async def set_date(self, field_id: str, value: Any) -> dict[str, Any]:
        target, fmt = self._parse_date_value(value)
        display = target.strftime("%Y-%m") if fmt == "yyyy-MM" else target.isoformat()
        locator, field = await self._field(field_id)
        monthish = bool(field.get("needs_month")) or bool(
            re.search(r"入学年月|毕业年月|预计毕业|年月", str(field.get("label") or ""))
        )
        if monthish and fmt == "yyyy-MM-dd":
            display = target.strftime("%Y-%m")
            fmt = "yyyy-MM"
        if field["type"] == "date" and not field["read_only"] and not field.get("disabled"):
            await locator.fill(target.isoformat())
            verified = await self.verify(field_id, target.isoformat())
            return {"ok": verified["matches"], "field_id": field_id, "mode": "native"}

        # Prefer direct write for month-only readonly fields (WdatePicker often has no inspectable panel).
        if monthish or field.get("read_only") or field.get("disabled"):
            direct = await self._set_date_value_js(field_id, display)
            if direct.get("ok"):
                return direct

        opened = await self.open_field(field_id)
        if not opened["ok"]:
            # Last chance: write value even if opener failed.
            direct = await self._set_date_value_js(field_id, display)
            if direct.get("ok"):
                return direct
            return opened
        for _ in range(240):
            widget, calendar = await self._calendar_state()
            if calendar is None:
                direct = await self._set_date_value_js(field_id, display)
                if direct.get("ok"):
                    return direct
                return {"ok": False, "error": "打开字段后未识别到日期面板", "profile_hint": display}

            exact_dates = [
                item for item in widget["options"]
                if target.isoformat() in " ".join(str(item.get(key, "")) for key in ("value", "title", "aria_label"))
                and not item["disabled"]
            ]
            if len(exact_dates) == 1:
                await self._click_widget_item(exact_dates[0]["option_id"])
                actual = await locator.input_value()
                return {"ok": bool(actual), "field_id": field_id, "mode": "calendar", "has_value": bool(actual)}

            # Month-only panels: click year then month labels when present.
            if fmt == "yyyy-MM":
                month_labels = [
                    item for item in widget["options"]
                    if not item["disabled"]
                    and (
                        str(item.get("text") or "").strip() in {str(target.month), f"{target.month}月", f"{target.month:02d}"}
                        or str(item.get("value") or "") in {str(target.month), f"{target.month:02d}"}
                    )
                ]
                year_labels = [
                    item for item in widget["options"]
                    if not item["disabled"] and str(target.year) in " ".join(
                        str(item.get(key, "")) for key in ("text", "value", "title", "aria_label")
                    )
                ]
                current_year = calendar.get("current_year")
                if current_year == target.year and len(month_labels) == 1:
                    await self._click_widget_item(month_labels[0]["option_id"])
                    actual = await locator.input_value()
                    if actual:
                        return {"ok": True, "field_id": field_id, "mode": "calendar_month", "has_value": True}
                if current_year != target.year and len(year_labels) == 1:
                    await self._click_widget_item(year_labels[0]["option_id"])
                    continue

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
            matches = expected is None or actual == expected
        elif field.get("tag") == "select":
            actual = await locator.input_value()
            selected_text = await locator.evaluate(
                """(el) => {
                  const opt = el.selectedOptions && el.selectedOptions[0];
                  return opt ? String(opt.text || '').trim() : '';
                }"""
            )
            if expected is None:
                matches = True
            else:
                wanted = str(expected)
                matches = actual == wanted or selected_text == wanted
                if not matches:
                    # Match against option text when expected was a code/value.
                    matches = bool(
                        await locator.evaluate(
                            """(el, wanted) => {
                              const opt = Array.from(el.options || []).find(o => String(o.value) === String(wanted));
                              const selected = el.selectedOptions && el.selectedOptions[0];
                              if (!selected) return false;
                              if (String(selected.value) === String(wanted)) return true;
                              if (opt && selected.value === opt.value) return true;
                              return false;
                            }""",
                            wanted,
                        )
                    )
            # Placeholder still selected counts as failure when an expected value was provided.
            if expected is not None and re.search(r"请选择|^-+$", selected_text or ""):
                matches = False
            return {
                "ok": True,
                "field_id": field_id,
                "actual": actual,
                "selected_text": selected_text,
                "matches": matches,
            }
        else:
            actual = await locator.input_value()
            matches = expected is None or actual == expected
        return {
            "ok": True,
            "field_id": field_id,
            "actual": actual,
            "matches": matches,
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
