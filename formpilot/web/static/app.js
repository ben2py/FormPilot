const SECTION_META = [
  { key: "identity", title: "身份信息" },
  { key: "contact", title: "联系方式" },
  { key: "education", title: "教育信息" },
  { key: "birthplace", title: "出生地" },
  { key: "origin", title: "籍贯 / 户口" },
  { key: "archive", title: "档案信息" },
  { key: "application", title: "报考意向" },
  { key: "family", title: "家庭成员", nested: true },
  { key: "language", title: "外语水平" },
  { key: "computer", title: "计算机水平" },
  { key: "experience", title: "学习与工作经历", nested: true },
  { key: "publications", title: "学术成果", nested: true },
  { key: "awards", title: "奖励", nested: true },
  { key: "documents", title: "本地材料路径" },
  { key: "extras", title: "其他" },
];

const LABEL_MAP = {
  "identity.full_name_zh": "中文姓名",
  "identity.name_pinyin": "姓名拼音",
  "identity.document_type": "证件类型",
  "identity.document_number": "证件号码",
  "identity.gender": "性别",
  "identity.birth_date": "出生日期",
  "identity.nationality": "国籍",
  "identity.ethnicity": "民族",
  "identity.marital_status": "婚姻状况",
  "identity.political_status": "政治面貌",
  "identity.military_status": "军人情况",
  "contact.mobile": "手机",
  "contact.email": "邮箱",
  "contact.address": "通信地址",
  "contact.postal_code": "邮编",
  "education.school": "学校",
  "education.province": "院校省份",
  "education.major": "专业",
  "education.major_category": "专业门类",
  "education.degree": "学位",
  "education.enrollment_date": "入学年月",
  "education.graduation_date": "毕业年月",
  "education.department": "院系",
  "education.gpa": "绩点",
  "education.score_percent": "百分制成绩",
  "education.rank_position": "排名名次",
  "education.rank_total": "排名总人数",
  "education.student_id": "学号",
  "documents.photo": "证件照路径",
  "documents.english": "英语成绩证明",
  "documents.id_card": "身份证",
  "documents.transcript": "成绩单",
  "documents.student_status": "学籍验证报告",
  "documents.resume": "简历",
  "documents.awards_research": "奖项学术成果",
};

let profileState = {};
let currentInteraction = null;

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2400);
}

function logLine(text, cls = "") {
  const box = $("console");
  const line = document.createElement("div");
  line.className = `log-line ${cls}`.trim();
  line.textContent = text;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function labelFor(path, key) {
  if (LABEL_MAP[path]) return LABEL_MAP[path];
  const parts = path.split(".");
  if (parts.length >= 3) {
    return `${parts.slice(1).join(" · ")}`;
  }
  return key;
}

function flattenFields(obj, prefix = "") {
  const rows = [];
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return rows;
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      rows.push(...flattenFields(value, path));
    } else {
      rows.push({ path, key, value: value == null ? "" : String(value) });
    }
  }
  return rows;
}

function setByPath(root, path, value) {
  const parts = path.split(".");
  let cur = root;
  for (let i = 0; i < parts.length - 1; i += 1) {
    const part = parts[i];
    if (!cur[part] || typeof cur[part] !== "object") cur[part] = {};
    cur = cur[part];
  }
  cur[parts[parts.length - 1]] = value;
}

function renderSection(host, key, title, data, { open = false } = {}) {
  if (data == null) return;
  const details = document.createElement("details");
  details.className = "section";
  details.open = open;
  const summary = document.createElement("summary");
  summary.textContent = title;
  details.appendChild(summary);
  const body = document.createElement("div");
  body.className = "section-body";
  const fields = flattenFields({ [key]: data });
  for (const field of fields) {
    const wrap = document.createElement("label");
    wrap.className = "field";
    const span = document.createElement("span");
    span.textContent = labelFor(field.path, field.key);
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value;
    input.dataset.path = field.path;
    input.addEventListener("input", () => {
      setByPath(profileState, field.path, input.value);
    });
    wrap.append(span, input);
    body.appendChild(wrap);
  }
  details.appendChild(body);
  host.appendChild(details);
}

function renderProfileEditor(profile) {
  profileState = structuredClone(profile || {});
  const host = $("profileEditor");
  host.innerHTML = "";
  const seen = new Set();
  const openKeys = new Set(["identity", "contact", "education", "family", "documents"]);
  for (const section of SECTION_META) {
    seen.add(section.key);
    renderSection(host, section.key, section.title, profileState[section.key], {
      open: openKeys.has(section.key),
    });
  }
  for (const key of Object.keys(profileState)) {
    if (seen.has(key)) continue;
    renderSection(host, key, key, profileState[key]);
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

async function loadAll() {
  const [profile, task, settings] = await Promise.all([
    api("/api/profile"),
    api("/api/task"),
    api("/api/settings"),
  ]);
  renderProfileEditor(profile.profile);
  $("taskEditor").value = task.content || "";
  $("modelPill").textContent = settings.model || "model";
  setStatus(settings.has_api_key ? "Idle" : "Missing API key", settings.has_api_key ? "" : "error");
}

function setStatus(text, cls = "") {
  const pill = $("statusPill");
  pill.textContent = text;
  pill.className = `pill ${cls}`.trim();
}

function collectGuidance() {
  return ($("runGuidance").value || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function showModal(interaction) {
  currentInteraction = interaction;
  const kind = interaction.kind;
  const payload = interaction.payload || {};
  $("modal").hidden = false;
  $("modalFields").hidden = true;
  $("modalFields").innerHTML = "";
  $("modalGuidanceWrap").hidden = true;
  $("modalGuidance").value = "";

  if (kind === "confirm") {
    $("modalKicker").textContent = "需要确认";
    $("modalTitle").textContent = "批准本次操作？";
    $("modalBody").textContent = payload.summary || "";
    $("modalAllow").textContent = "允许";
    $("modalDeny").textContent = "拒绝";
  } else if (kind === "pause") {
    $("modalKicker").textContent = "需要你接管";
    $("modalTitle").textContent = "请在浏览器完成操作";
    $("modalBody").textContent = payload.message || "";
    $("modalGuidanceWrap").hidden = false;
    $("modalAllow").textContent = "我已完成，继续";
    $("modalDeny").textContent = "跳过指引继续";
  } else if (kind === "missing") {
    $("modalKicker").textContent = "补充资料";
    $("modalTitle").textContent = "请补全以下字段";
    $("modalBody").textContent = "这些信息无法从现有 profile 可靠推出。";
    $("modalFields").hidden = false;
    for (const field of payload.fields || []) {
      const wrap = document.createElement("label");
      wrap.className = "field";
      const span = document.createElement("span");
      span.textContent = `${field.label || field.profile_path}${field.hint ? ` · ${field.hint}` : ""}`;
      const input = document.createElement("input");
      input.type = "text";
      input.dataset.path = field.profile_path;
      wrap.append(span, input);
      $("modalFields").appendChild(wrap);
    }
    $("modalAllow").textContent = "保存并继续";
    $("modalDeny").textContent = "全部跳过";
  }
}

async function resolveModal(allow) {
  if (!currentInteraction) return;
  const id = currentInteraction.id;
  const kind = currentInteraction.kind;
  let answer;
  if (kind === "confirm") answer = Boolean(allow);
  else if (kind === "pause") answer = allow ? ($("modalGuidance").value || "") : "";
  else {
    answer = {};
    if (allow) {
      for (const input of $("modalFields").querySelectorAll("input[data-path]")) {
        const val = input.value.trim();
        if (val) answer[input.dataset.path] = val;
      }
    }
  }
  $("modal").hidden = true;
  currentInteraction = null;
  await api(`/api/interact/${id}`, { method: "POST", body: JSON.stringify({ answer }) });
  logLine(`[interaction] resolved ${kind}`, "event");
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch { return; }
    if (data.type === "ping" || data.type === "hello") {
      if (data.status) setStatus(String(data.status), data.status === "running" ? "running" : "");
      return;
    }
    if (data.type === "trace") {
      logLine(data.message || "", "");
      return;
    }
    if (data.type === "interaction") {
      logLine(`[interaction] ${data.kind}`, "event");
      showModal(data);
      return;
    }
    if (data.type === "run_started") {
      setStatus("Running", "running");
      logLine(`Run started → ${data.url}`, "event");
      return;
    }
    if (data.type === "run_finished") {
      setStatus("Done", "done");
      logLine(data.text || "Finished", "ok");
      return;
    }
    if (data.type === "run_error") {
      setStatus("Error", "error");
      logLine(data.error || "error", "error");
      return;
    }
    if (data.type === "run_stopped") {
      setStatus("Stopped", "error");
      logLine("Run stopped", "event");
    }
  };
}

$("saveProfile").addEventListener("click", async () => {
  try {
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ profile: profileState }) });
    toast("资料已保存");
  } catch (err) {
    toast(err.message);
  }
});

$("reloadProfile").addEventListener("click", () => loadAll().then(() => toast("已重新加载")));
$("saveTask").addEventListener("click", async () => {
  try {
    await api("/api/task", { method: "PUT", body: JSON.stringify({ content: $("taskEditor").value }) });
    toast("任务说明已保存");
  } catch (err) {
    toast(err.message);
  }
});
$("reloadTask").addEventListener("click", async () => {
  const task = await api("/api/task");
  $("taskEditor").value = task.content || "";
  toast("任务已重新加载");
});

$("startRun").addEventListener("click", async () => {
  try {
    await api("/api/profile", { method: "PUT", body: JSON.stringify({ profile: profileState }) });
    await api("/api/task", { method: "PUT", body: JSON.stringify({ content: $("taskEditor").value }) });
    const body = {
      url: $("runUrl").value.trim(),
      guidance: collectGuidance(),
      headless: $("runHeadless").checked,
    };
    if (!body.url) throw new Error("请先填写目标 URL");
    await api("/api/run", { method: "POST", body: JSON.stringify(body) });
    setStatus("Starting", "running");
    logLine("Starting agent…", "event");
  } catch (err) {
    toast(err.message);
    logLine(err.message, "error");
  }
});

$("stopRun").addEventListener("click", async () => {
  await api("/api/run/stop", { method: "POST", body: "{}" });
});

$("clearLog").addEventListener("click", () => { $("console").innerHTML = ""; });
$("modalAllow").addEventListener("click", () => resolveModal(true));
$("modalDeny").addEventListener("click", () => resolveModal(false));

loadAll().catch((err) => toast(err.message));
connectEvents();
