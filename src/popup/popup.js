(function () {
  "use strict";

  const core = globalThis.FormPilotCore;
  const state = { tab: null, page: null, profile: {}, matches: [] };
  let saveTimer;

  const elements = {
    siteName: document.getElementById("siteName"),
    siteOrigin: document.getElementById("siteOrigin"),
    scanButton: document.getElementById("scanButton"),
    profileForm: document.getElementById("profileForm"),
    profileSummary: document.getElementById("profileSummary"),
    resultTitle: document.getElementById("resultTitle"),
    readyCount: document.getElementById("readyCount"),
    resultList: document.getElementById("resultList"),
    fillButton: document.getElementById("fillButton"),
    statusText: document.getElementById("statusText")
  };

  function setStatus(text, kind) {
    elements.statusText.textContent = text;
    elements.statusText.className = `status-text${kind ? ` ${kind}` : ""}`;
  }

  function storageGet(key) {
    return new Promise((resolve) => chrome.storage.local.get(key, resolve));
  }

  function storageSet(value) {
    return new Promise((resolve) => chrome.storage.local.set(value, resolve));
  }

  function activeTab() {
    return new Promise((resolve) => chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => resolve(tabs[0])));
  }

  function send(message) {
    return new Promise((resolve, reject) => {
      if (!state.tab?.id) return reject(new Error("没有可用的网页标签页"));
      chrome.tabs.sendMessage(state.tab.id, message, (response) => {
        if (chrome.runtime.lastError) reject(new Error("当前页面不允许扩展读取，请打开普通网页后重试"));
        else resolve(response);
      });
    });
  }

  function profileInputs() {
    return Array.from(elements.profileForm.querySelectorAll("[data-path]"));
  }

  function readProfileForm() {
    const profile = {};
    profileInputs().forEach((input) => core.setPath(profile, input.dataset.path, input.value.trim()));
    return profile;
  }

  function updateProfileSummary() {
    const count = profileInputs().filter((input) => input.value.trim()).length;
    elements.profileSummary.textContent = count ? `已保存 ${count} 项 · 仅在此浏览器` : "填写后仅保存在此浏览器";
  }

  function loadProfileForm() {
    profileInputs().forEach((input) => { input.value = core.getPath(state.profile, input.dataset.path) || ""; });
    updateProfileSummary();
  }

  function scheduleProfileSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      state.profile = readProfileForm();
      await storageSet({ formpilotProfile: state.profile });
      updateProfileSummary();
      if (state.page) renderMatches();
    }, 250);
  }

  function hasCurrentValue(match) {
    const value = match.field.currentValue;
    if (match.field.type === "checkbox" || match.field.type === "radio") return value === true;
    return String(value || "").trim() !== "";
  }

  function visibleMatches(matches) {
    const actionable = matches.filter((match) => ["ready", "review", "waiting", "blocked"].includes(match.status));
    return actionable.slice(0, 80);
  }

  function resultItem(match) {
    const row = document.createElement("label");
    const existing = hasCurrentValue(match);
    const selectable = ["ready", "review"].includes(match.status) && !match.field.disabled && !match.field.readOnly && !existing;
    row.className = `result-item${existing ? " existing" : ""}`;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "match-select";
    checkbox.dataset.fieldId = match.field.fieldId;
    checkbox.checked = selectable && match.status === "ready";
    checkbox.disabled = !selectable;

    const copy = document.createElement("span");
    copy.className = "result-copy";
    const label = document.createElement("span");
    label.className = "result-label";
    label.textContent = match.field.label;
    const value = document.createElement("span");
    value.className = "result-value";
    if (existing) value.textContent = "已有内容，默认不覆盖";
    else if (match.value !== undefined) value.textContent = `${match.profileLabel} → ${core.maskValue(match.profilePath, match.displayValue)}`;
    else value.textContent = match.reason || "需要检查";
    copy.append(label, value);

    const badge = document.createElement("span");
    if (match.status === "blocked") {
      badge.className = "confidence blocked";
      badge.textContent = "保护";
    } else if (match.status === "waiting") {
      badge.className = "confidence review";
      badge.textContent = "等待选项";
    } else {
      badge.className = `confidence${match.status === "review" ? " review" : ""}`;
      badge.textContent = `${Math.round(match.confidence * 100)}%`;
    }

    row.append(checkbox, copy, badge);
    return row;
  }

  function selectedMatches() {
    const ids = new Set(Array.from(elements.resultList.querySelectorAll(".match-select:checked")).map((input) => input.dataset.fieldId));
    return state.matches.filter((match) => ids.has(match.field.fieldId));
  }

  function updateFillButton() {
    const count = selectedMatches().length;
    elements.fillButton.disabled = count === 0;
    elements.fillButton.firstChild.textContent = count ? `确认并填写 ${count} 项 ` : "确认并填写 ";
  }

  function renderMatches() {
    state.profile = readProfileForm();
    state.matches = core.matchFields(state.page?.fields || [], state.profile);
    const visible = visibleMatches(state.matches);
    const ready = state.matches.filter((match) => ["ready", "review"].includes(match.status) && !hasCurrentValue(match));

    elements.resultList.replaceChildren();
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "识别到了表单，但资料库中还没有可匹配的值。请展开“个人资料”填写。";
      elements.resultList.appendChild(empty);
    } else visible.forEach((match) => elements.resultList.appendChild(resultItem(match)));

    elements.resultTitle.textContent = `识别 ${state.page?.fields?.length || 0} 个字段`;
    elements.readyCount.textContent = String(ready.length);
    updateFillButton();
    elements.resultList.querySelectorAll(".match-select").forEach((input) => input.addEventListener("change", updateFillButton));
    send({ type: "FORMPILOT_HIGHLIGHT", fieldIds: ready.map((match) => match.field.fieldId) }).catch(() => {});
  }

  async function scan() {
    setStatus("正在分析当前网页…");
    elements.scanButton.disabled = true;
    try {
      state.tab = await activeTab();
      const response = await send({ type: "FORMPILOT_SCAN" });
      if (!response?.ok) throw new Error("没有读取到表单");
      state.page = response;
      const parsed = new URL(response.url);
      elements.siteName.textContent = response.title || parsed.hostname;
      elements.siteOrigin.textContent = parsed.origin;
      renderMatches();
      setStatus("只填写勾选项，不会点击“提交”");
    } catch (error) {
      elements.siteName.textContent = "无法读取当前页面";
      elements.siteOrigin.textContent = "浏览器内部页、扩展商店等页面不受支持";
      elements.resultTitle.textContent = "等待可访问的表单";
      elements.resultList.innerHTML = '<div class="empty-state">请打开普通 HTTP/HTTPS 表单页面，然后点击重新扫描。</div>';
      elements.fillButton.disabled = true;
      setStatus(error.message, "error");
    } finally {
      elements.scanButton.disabled = false;
    }
  }

  async function fillSelected() {
    const selected = selectedMatches();
    if (!selected.length) return;
    const destination = state.page ? new URL(state.page.url).origin : "当前网站";
    const highRisk = selected.filter((match) => match.risk === "high");
    const detail = highRisk.length ? `\n其中 ${highRisk.length} 项包含联系方式、证件号或地址。` : "";
    if (!window.confirm(`将向 ${destination} 填写 ${selected.length} 项资料。${detail}\n\nFormPilot 不会提交表单。`)) return;

    elements.fillButton.disabled = true;
    setStatus("正在填写并回读验证…");
    try {
      const response = await send({
        type: "FORMPILOT_FILL",
        items: selected.map((match) => ({ fieldId: match.field.fieldId, value: match.value }))
      });
      const failed = (response?.results || []).filter((result) => !result.ok);
      state.page.fields = response?.fields || state.page.fields;
      renderMatches();
      if (failed.length) setStatus(`已填写 ${selected.length - failed.length} 项，${failed.length} 项需人工检查`, "error");
      else setStatus(`已填写并验证 ${selected.length} 项；动态字段可再次扫描`, "success");
    } catch (error) {
      setStatus(error.message, "error");
    } finally {
      updateFillButton();
    }
  }

  async function init() {
    const saved = await storageGet("formpilotProfile");
    state.profile = saved.formpilotProfile || {};
    loadProfileForm();
    profileInputs().forEach((input) => input.addEventListener("input", scheduleProfileSave));
    elements.scanButton.addEventListener("click", scan);
    elements.fillButton.addEventListener("click", fillSelected);
    await scan();
  }

  init();
})();
