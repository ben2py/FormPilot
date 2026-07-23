from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .credentials import match_select_option


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
      return cleanLabelText(prev) || compact(prev.innerText || prev.textContent);
    }
    const row = td.parentElement;
    if (row) {
      const th = Array.from(row.children).find(x => x.tagName === "TH");
      if (th && th !== td) return cleanLabelText(th) || compact(th.innerText || th.textContent);
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
    return !text || /^(请选择|点击选择|选择|请填写)/.test(text) || text === "-" || text === "--" || text === "—";
  };
  const looksLikeRegionLabel = (label) => /出生地|籍贯|户口|所在地|省市|地区|归属地|生源地/.test(String(label || ""));

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
      const needsCascade = readOnly && (looksLikeRegionLabel(label) || isPlaceholderValue(el.getAttribute("placeholder")) || /area|region|city|cascade|picker/i.test(`${el.id} ${el.name} ${el.className || ""}`));
      return {
        field_id: idFor(el, "field"), tag, type, id: el.id || "", name: el.name || "",
        label, placeholder: el.getAttribute("placeholder") || "",
        required: isRequired(el),
        disabled: Boolean(el.disabled), read_only: readOnly,
        needs_cascade: needsCascade,
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
            option = match_select_option(options, text_value)
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
        # Readonly region pickers are intentionally clickable; only skip inert disabled
        # fields that are not marked as cascade targets.
        if field.get("disabled") and not (field.get("read_only") or field.get("needs_cascade")):
            return {"ok": False, "error": "字段不可交互"}
        clicked = False
        try:
            await locator.click(force=True, timeout=2000)
            clicked = True
        except Exception:
            clicked = False
        if not clicked:
            # Some Tongji/layui pickers put the handler on the parent cell / sibling.
            clicked = bool(
                await self.page.evaluate(
                    """(id) => {
                      const el = document.querySelector(`[data-formpilot-id="${id}"]`);
                      if (!el) return false;
                      const candidates = [
                        el,
                        el.parentElement,
                        el.closest("td"),
                        el.nextElementSibling,
                        el.previousElementSibling,
                      ].filter(Boolean);
                      for (const node of candidates) {
                        try { node.click(); return true; } catch (e) {}
                      }
                      return false;
                    }""",
                    field_id,
                )
            )
        if not clicked:
            return {"ok": False, "error": "无法打开字段选择器"}
        await self.page.wait_for_timeout(400)
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
        await locator.click(force=True)
        await self.page.wait_for_timeout(250)

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

    async def select_cascade(self, field_id: str, path: list[Any]) -> dict[str, Any]:
        if not path:
            return {"ok": False, "error": "级联路径为空"}
        opened = await self.open_field(field_id)
        if not opened["ok"]:
            return opened
        selected_count = 0
        for level, raw_value in enumerate(path):
            widget = await self.inspect_widget()
            candidates = self._match_cascade_option(widget["options"], raw_value, level)
            if len(candidates) != 1:
                # Unique-by-shortest-text when several soft matches share a prefix.
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
                        "level": level,
                        "wanted": str(raw_value),
                        "candidate_count": len(candidates),
                        "available_options": [
                            item["text"] for item in widget["options"] if not item["disabled"]
                        ][:100],
                    }
            await self._click_widget_item(candidates[0]["option_id"])
            selected_count += 1
            await self.page.wait_for_timeout(200)
        # Prefer fresh inspect has_value; some pickers mirror text into siblings.
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
