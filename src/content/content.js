(function () {
  "use strict";

  const core = globalThis.FormPilotCore;
  const FIELD_ATTRIBUTE = "data-formpilot-field-id";
  const HIGHLIGHT_CLASS = "formpilot-candidate";
  let sequence = 0;

  function isVisible(element) {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0" && element.getClientRects().length > 0;
  }

  function compactText(value, maxLength) {
    return String(value || "").replace(/\s+/g, " ").trim().slice(0, maxLength);
  }

  function explicitLabel(element) {
    if (!element.id) return "";
    const label = Array.from(document.querySelectorAll("label[for]")).find((item) => item.getAttribute("for") === element.id);
    return compactText(label && label.innerText, 100);
  }

  function labelledByText(element) {
    const ids = String(element.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    return compactText(ids.map((id) => document.getElementById(id)?.innerText || "").join(" "), 100);
  }

  function nearbyText(element) {
    const closestLabel = element.closest("label");
    if (closestLabel) return compactText(closestLabel.innerText, 100);

    const siblingTexts = [element.previousElementSibling, element.nextElementSibling]
      .map((item) => compactText(item && item.innerText, 60))
      .filter(Boolean);
    if (siblingTexts.length) return siblingTexts.join(" ");

    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 4; depth += 1, parent = parent.parentElement) {
      const text = compactText(parent.innerText, 140);
      if (text && text.length <= 120) return text;
    }
    return "";
  }

  function sectionText(element) {
    const section = element.closest("fieldset, section, [role='group'], .form-section, .panel, .card");
    if (!section) return "";
    const heading = section.querySelector("legend, h1, h2, h3, h4, .title, .header");
    return compactText(heading && heading.innerText, 80);
  }

  function ensureFieldId(element) {
    if (!element.hasAttribute(FIELD_ATTRIBUTE)) {
      sequence += 1;
      element.setAttribute(FIELD_ATTRIBUTE, `fp-${Date.now().toString(36)}-${sequence}`);
    }
    return element.getAttribute(FIELD_ATTRIBUTE);
  }

  function readField(element) {
    const tag = element.tagName.toLowerCase();
    const type = tag === "select" ? "select" : (element.getAttribute("type") || tag).toLowerCase();
    const ariaLabel = element.getAttribute("aria-label") || "";
    const label = explicitLabel(element) || labelledByText(element) || ariaLabel || nearbyText(element);
    const optionLabel = type === "radio" || type === "checkbox" ? compactText(element.closest("label")?.innerText || nearbyText(element), 80) : "";

    return {
      fieldId: ensureFieldId(element),
      tag,
      type,
      id: element.id || "",
      name: element.getAttribute("name") || "",
      label: label || element.getAttribute("placeholder") || element.getAttribute("name") || element.id || "未命名字段",
      placeholder: element.getAttribute("placeholder") || "",
      ariaLabel,
      section: sectionText(element),
      required: element.required || element.getAttribute("aria-required") === "true",
      disabled: element.disabled || element.getAttribute("aria-disabled") === "true",
      readOnly: Boolean(element.readOnly),
      currentValue: type === "checkbox" || type === "radio" ? Boolean(element.checked) : String(element.value || ""),
      optionValue: type === "radio" || type === "checkbox" ? String(element.value || "") : undefined,
      optionLabel: optionLabel || undefined,
      options: tag === "select" ? Array.from(element.options).slice(0, 200).map((option) => ({
        value: option.value,
        text: compactText(option.text, 120),
        selected: option.selected,
        disabled: option.disabled
      })) : undefined
    };
  }

  function scanFields() {
    const selector = "input, select, textarea, [contenteditable='true']";
    return Array.from(document.querySelectorAll(selector))
      .filter((element) => isVisible(element))
      .slice(0, 300)
      .map(readField);
  }

  function installHighlightStyles() {
    if (document.getElementById("formpilot-highlight-style")) return;
    const style = document.createElement("style");
    style.id = "formpilot-highlight-style";
    style.textContent = `.${HIGHLIGHT_CLASS}{outline:2px solid #6d5dfc!important;outline-offset:2px!important;transition:outline-color .2s ease}`;
    document.documentElement.appendChild(style);
  }

  function clearHighlights() {
    document.querySelectorAll(`.${HIGHLIGHT_CLASS}`).forEach((element) => element.classList.remove(HIGHLIGHT_CLASS));
  }

  function highlight(fieldIds) {
    installHighlightStyles();
    clearHighlights();
    fieldIds.forEach((fieldId) => {
      const element = document.querySelector(`[${FIELD_ATTRIBUTE}="${fieldId}"]`);
      if (element) element.classList.add(HIGHLIGHT_CLASS);
    });
  }

  function nativeSetter(element, property, value) {
    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(element), property);
    if (descriptor && descriptor.set) descriptor.set.call(element, value);
    else element[property] = value;
  }

  function dispatchEditEvents(element) {
    element.dispatchEvent(new Event("input", { bubbles: true }));
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function fillOne(item) {
    const element = document.querySelector(`[${FIELD_ATTRIBUTE}="${item.fieldId}"]`);
    if (!element || !isVisible(element)) return { fieldId: item.fieldId, ok: false, error: "字段已消失或不可见" };
    if (element.disabled || element.readOnly) return { fieldId: item.fieldId, ok: false, error: "字段不可编辑" };

    const current = readField(element);
    const safetyReason = core.blockedReason(current);
    if (safetyReason) return { fieldId: item.fieldId, ok: false, error: safetyReason };

    try {
      if (element.tagName === "SELECT") nativeSetter(element, "value", String(item.value));
      else if (current.type === "checkbox" || current.type === "radio") nativeSetter(element, "checked", Boolean(item.value));
      else if (element.getAttribute("contenteditable") === "true") element.textContent = String(item.value);
      else nativeSetter(element, "value", String(item.value));
      dispatchEditEvents(element);

      const after = readField(element);
      const expected = current.type === "checkbox" || current.type === "radio" ? Boolean(item.value) : String(item.value);
      const ok = after.currentValue === expected;
      return { fieldId: item.fieldId, ok, error: ok ? undefined : "页面未接受该值" };
    } catch (error) {
      return { fieldId: item.fieldId, ok: false, error: error instanceof Error ? error.message : "填写失败" };
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message || typeof message.type !== "string") return false;

    if (message.type === "FORMPILOT_SCAN") {
      const fields = scanFields();
      sendResponse({ ok: true, title: document.title, url: location.href, fields });
      return false;
    }

    if (message.type === "FORMPILOT_HIGHLIGHT") {
      highlight(Array.isArray(message.fieldIds) ? message.fieldIds : []);
      sendResponse({ ok: true });
      return false;
    }

    if (message.type === "FORMPILOT_FILL") {
      const items = Array.isArray(message.items) ? message.items : [];
      const results = items.map(fillOne);
      highlight(results.filter((result) => !result.ok).map((result) => result.fieldId));
      sendResponse({ ok: results.every((result) => result.ok), results, fields: scanFields() });
      return false;
    }

    return false;
  });
})();
