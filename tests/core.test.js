const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../src/core/formpilot-core.js");

const profile = {
  identity: {
    full_name_zh: "张三",
    document_type: "居民身份证",
    document_number: "310101199001011234"
  },
  contact: { mobile: "13800138000", email: "zhangsan@example.com" },
  application: { batch: "2026年秋季批次" }
};

test("normalizes Chinese and punctuation consistently", () => {
  assert.equal(core.normalizeText(" 证件-类型： "), "证件类型");
  assert.equal(core.normalizeText("E-mail Address"), "emailaddress");
});

test("maps a target-style name field", () => {
  const result = core.matchField({ tag: "input", type: "text", id: "xm", name: "xm", label: "姓名", placeholder: "请输入真实姓名" }, profile);
  assert.equal(result.status, "ready");
  assert.equal(result.profilePath, "identity.full_name_zh");
  assert.equal(result.value, "张三");
  assert.ok(result.confidence >= 0.9);
});

test("resolves a select label to its real option value", () => {
  const result = core.matchField({
    tag: "select", type: "select", id: "zjlx", label: "证件类型",
    options: [{ text: "--请选择--", value: "" }, { text: "居民身份证", value: "01" }]
  }, profile);
  assert.equal(result.status, "ready");
  assert.equal(result.value, "01");
  assert.equal(result.displayValue, "居民身份证");
});

test("waits instead of inventing a dynamic select option", () => {
  const result = core.matchField({
    tag: "select", type: "select", id: "pcid", label: "招生批次",
    options: [{ text: "--请选择--", value: "" }]
  }, profile);
  assert.equal(result.status, "waiting");
  assert.match(result.reason, /没有对应值/);
});

test("blocks passwords and verification codes", () => {
  assert.equal(core.matchField({ tag: "input", type: "password", label: "密码" }, profile).status, "blocked");
  assert.equal(core.matchField({ tag: "input", type: "text", label: "短信验证码" }, profile).status, "blocked");
});

test("does not match unrelated fields", () => {
  const result = core.matchField({ tag: "textarea", type: "textarea", label: "个人陈述" }, profile);
  assert.equal(result.status, "unmatched");
});

test("masks high-risk values in the review UI", () => {
  assert.equal(core.maskValue("contact.mobile", "13800138000"), "138••••8000");
  assert.equal(core.maskValue("identity.document_number", "310101199001011234"), "310••••••1234");
  assert.equal(core.maskValue("contact.email", "zhangsan@example.com"), "zh•••@example.com");
});
