(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.FormPilotCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const FIELD_DEFINITIONS = [
    { path: "identity.full_name_zh", label: "姓名", aliases: ["姓名", "真实姓名", "考生姓名", "中文姓名", "name"] },
    { path: "identity.document_type", label: "证件类型", aliases: ["证件类型", "证件类别", "身份证件类型"] },
    { path: "identity.document_number", label: "证件号码", aliases: ["证件号码", "证件号", "身份证号", "身份证号码", "证号"], risk: "high" },
    { path: "identity.gender", label: "性别", aliases: ["性别", "gender"] },
    { path: "identity.birth_date", label: "出生日期", aliases: ["出生日期", "出生年月", "生日", "birthdate"] },
    { path: "identity.nationality", label: "国籍/地区", aliases: ["国籍", "国家地区", "国籍地区"] },
    { path: "identity.ethnicity", label: "民族", aliases: ["民族"] },
    { path: "contact.mobile", label: "手机号码", aliases: ["手机号码", "手机号", "移动电话", "联系电话", "手机"], risk: "high" },
    { path: "contact.email", label: "电子邮箱", aliases: ["电子邮箱", "邮箱", "email", "emailaddress"], risk: "high" },
    { path: "contact.address", label: "通信地址", aliases: ["通信地址", "通讯地址", "联系地址", "邮寄地址"], risk: "high" },
    { path: "contact.postal_code", label: "邮政编码", aliases: ["邮政编码", "邮编"] },
    { path: "education.school", label: "毕业/在读院校", aliases: ["毕业院校", "本科院校", "就读院校", "所在学校", "学校名称"] },
    { path: "education.major", label: "所学专业", aliases: ["所学专业", "本科专业", "毕业专业", "专业名称"] },
    { path: "education.degree", label: "学位", aliases: ["学位", "学历学位", "最高学位"] },
    { path: "education.graduation_date", label: "毕业日期", aliases: ["毕业日期", "毕业年月", "预计毕业时间"] },
    { path: "application.project", label: "招生项目", aliases: ["招生项目", "报考项目", "申请项目"] },
    { path: "application.batch", label: "招生批次", aliases: ["招生批次", "报名批次", "申请批次", "批次"] },
    { path: "application.mode", label: "招生方式", aliases: ["招生方式", "报考方式", "申请方式"] },
    { path: "application.program", label: "报考专业", aliases: ["报考专业", "申请专业", "意向专业", "招生专业"] }
  ];

  const BLOCKED_PATTERNS = [
    "验证码", "captcha", "短信码", "动态码", "密码", "口令", "服务协议", "隐私政策", "阅读并同意", "承诺", "签名"
  ];

  function normalizeText(value) {
    return String(value == null ? "" : value)
      .toLowerCase()
      .replace(/[\s\-—_:/：;；,.，。()（）【】\[\]<>《》'\"`]/g, "");
  }

  function getPath(object, path) {
    return path.split(".").reduce((value, key) => value == null ? undefined : value[key], object);
  }

  function setPath(object, path, value) {
    const keys = path.split(".");
    let cursor = object;
    keys.slice(0, -1).forEach((key) => {
      if (!cursor[key] || typeof cursor[key] !== "object") cursor[key] = {};
      cursor = cursor[key];
    });
    cursor[keys[keys.length - 1]] = value;
    return object;
  }

  function fieldSignature(field) {
    return normalizeText([
      field.label,
      field.placeholder,
      field.name,
      field.id,
      field.ariaLabel,
      field.section
    ].filter(Boolean).join(" "));
  }

  function blockedReason(field) {
    const type = normalizeText(field.type);
    const signature = fieldSignature(field);
    if (["password", "file", "hidden"].includes(type)) return `不自动处理 ${type || "隐藏"} 控件`;
    const pattern = BLOCKED_PATTERNS.find((item) => signature.includes(normalizeText(item)));
    if (pattern) return `安全保护：${pattern} 需要用户处理`;
    return null;
  }

  function compatibilityScore(field, definition, value) {
    const signature = fieldSignature(field);
    const label = normalizeText(field.label || field.placeholder || "");
    const leaf = normalizeText(definition.path.split(".").pop());
    let best = 0;

    definition.aliases.forEach((alias) => {
      const normalizedAlias = normalizeText(alias);
      if (!normalizedAlias) return;
      if (label === normalizedAlias) best = Math.max(best, 0.98);
      else if (label.includes(normalizedAlias) || normalizedAlias.includes(label) && label.length >= 2) best = Math.max(best, 0.91);
      else if (signature.includes(normalizedAlias)) best = Math.max(best, 0.84);
    });

    if ([normalizeText(field.name), normalizeText(field.id)].includes(leaf)) best = Math.max(best, 0.94);
    if (best === 0) return 0;

    const type = normalizeText(field.type);
    if (type === "email" && definition.path !== "contact.email") best -= 0.25;
    if (type === "tel" && definition.path !== "contact.mobile") best -= 0.2;
    if (type === "date" && !definition.path.endsWith("date")) best -= 0.2;
    if (value == null || String(value).trim() === "") return 0;
    return Math.max(0, Math.min(0.99, best));
  }

  function resolveControlValue(field, value) {
    const stringValue = String(value);
    const wanted = normalizeText(stringValue);
    if (field.tag === "select") {
      const options = Array.isArray(field.options) ? field.options : [];
      const option = options.find((item) => normalizeText(item.text) === wanted || normalizeText(item.value) === wanted)
        || options.find((item) => {
          const text = normalizeText(item.text);
          return wanted.length >= 2 && (text.includes(wanted) || wanted.includes(text));
        });
      return option ? { ok: true, value: option.value, displayValue: option.text } : { ok: false, reason: "当前下拉选项中没有对应值" };
    }
    if (field.type === "radio") {
      const optionText = normalizeText(`${field.optionLabel || ""}${field.optionValue || ""}`);
      return optionText.includes(wanted) || wanted.includes(optionText)
        ? { ok: true, value: field.optionValue, displayValue: stringValue }
        : { ok: false, reason: "单选项与资料值不一致" };
    }
    if (field.type === "checkbox") {
      if (typeof value !== "boolean") return { ok: false, reason: "复选框需要布尔值" };
      return { ok: true, value, displayValue: value ? "是" : "否" };
    }
    return { ok: true, value: stringValue, displayValue: stringValue };
  }

  function matchField(field, profile) {
    const safetyReason = blockedReason(field);
    if (safetyReason) return { field, status: "blocked", reason: safetyReason, confidence: 0 };

    const candidates = FIELD_DEFINITIONS.map((definition) => {
      const value = getPath(profile, definition.path);
      return { definition, value, score: compatibilityScore(field, definition, value) };
    }).filter((candidate) => candidate.score > 0).sort((a, b) => b.score - a.score);

    if (!candidates.length) return { field, status: "unmatched", reason: "未找到可靠的资料字段", confidence: 0 };
    const candidate = candidates[0];
    const resolved = resolveControlValue(field, candidate.value);
    if (!resolved.ok) {
      return {
        field,
        status: "waiting",
        reason: resolved.reason,
        confidence: candidate.score,
        profilePath: candidate.definition.path,
        profileLabel: candidate.definition.label,
        risk: candidate.definition.risk || "normal"
      };
    }

    return {
      field,
      status: candidate.score >= 0.9 ? "ready" : "review",
      confidence: candidate.score,
      profilePath: candidate.definition.path,
      profileLabel: candidate.definition.label,
      value: resolved.value,
      displayValue: resolved.displayValue,
      risk: candidate.definition.risk || "normal"
    };
  }

  function matchFields(fields, profile) {
    return fields.map((field) => matchField(field, profile || {}));
  }

  function maskValue(path, value) {
    const text = String(value == null ? "" : value);
    if (path === "identity.document_number" && text.length > 7) return `${text.slice(0, 3)}••••••${text.slice(-4)}`;
    if (path === "contact.mobile" && text.length >= 7) return `${text.slice(0, 3)}••••${text.slice(-4)}`;
    if (path === "contact.email" && text.includes("@")) {
      const [name, domain] = text.split("@");
      return `${name.slice(0, 2)}•••@${domain}`;
    }
    if (path === "contact.address" && text.length > 8) return `${text.slice(0, 6)}••••`;
    return text;
  }

  return {
    FIELD_DEFINITIONS,
    normalizeText,
    getPath,
    setPath,
    blockedReason,
    resolveControlValue,
    matchField,
    matchFields,
    maskValue
  };
});
