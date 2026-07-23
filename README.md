# FormPilot

**LLM-driven browser agent for graduate application forms.**  
Observe the page → decide the next tool → fill, verify, upload — with privacy-preserving profile handling and a local web console for human interaction.

---

## Highlights

| Capability | What you get |
|---|---|
| **Autonomous fill loop** | Model inspects live DOM, picks tools, verifies values, advances steps |
| **Local profile vault** | Real PII stays on disk; the model sees paths & types, not raw secrets |
| **Document pipeline** | Match local PDFs to page requirements, merge pages, upload with logs |
| **Web console** | Edit profile & task in the UI; confirm / pause / missing-fields as modals |
| **CLI** | Same agent for scripts, CI smoke, or headless runs |

---

## Architecture (short)

```
┌─────────────┐     tool calls      ┌──────────────────┐
│  LLM Agent  │ ◄──────────────────► │  FormPilotTools  │
└─────────────┘                      └────────┬─────────┘
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
             PlaywrightBrowser          ProfileStore              Policy / Approvals
             (observe & act)           (local JSON)              (confirm / block)
```

Each turn: the model returns a `function_call` → Python executes a constrained browser/profile tool → result is fed back until the agent finishes or hits `max_steps`.

Privacy defaults: page values are reported as `has_value` only; catalog entries omit raw strings; passwords, SMS codes, and submit clicks require explicit confirmation (or console modal approval).

---

## Quick start

**Requirements:** Python 3.11+

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

Bootstrap local secrets (never commit these):

```bash
cp .env.example .env
cp examples/profile.example.json profile.json
cp examples/task.example.md task.md
```

Set `OPENAI_API_KEY` in `.env`. Optional: point `OPENAI_BASE_URL` / `FORMPILOT_MODEL` at DeepSeek or other OpenAI-compatible endpoints.

---

## Web console (recommended)

Edit profile fields in the browser, start a run, and answer agent prompts via modals — no terminal `input()` required for confirm / pause / missing fields.

```bash
formpilot-web
# → http://127.0.0.1:8787
```

Override bind address if needed:

```bash
FORMPILOT_WEB_HOST=127.0.0.1 FORMPILOT_WEB_PORT=8787 formpilot-web
```

**Console flow**

1. Paste the target URL and fill **报考网站账号 / 密码** (saved to `.env` as `FORMPILOT_LOGIN_*`)  
2. Under **API 配置**, set the decision model and optional vision API → Save  
3. Fill **报名资料** and **任务说明** → Save  
4. Click **开始填表**  
5. When the agent needs you (SMS, confirm, missing field), a modal appears in the console  

Run traces still land in `.formpilot/logs/run-*.jsonl`.

---

## CLI

```bash
formpilot \
  --url "https://yzbm.tongji.edu.cn/..." \
  --profile profile.json \
  --task task.md
```

Natural-language guidance (repeatable):

```bash
formpilot \
  --url "https://example.com/form" \
  --profile profile.json \
  --task task.md \
  --guidance "有专硕优先选专硕" \
  --guidance "资料更新时覆盖重填"
```

Useful flags: `--model`, `--headless`, `--cdp-url`, `--env-file`, `--max-steps`.

---

## Configuration

| Variable | Role |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | Model endpoint |
| `FORMPILOT_MODEL` | Decision model (default balances tools vs cost) |
| `FORMPILOT_API_MODE` | `auto` · Responses vs Chat Completions |
| `FORMPILOT_VISION_*` | Optional VL model for captcha images |
| `FORMPILOT_FAST` / `FORMPILOT_WAIT_SCALE` | Speed vs stability |
| `FORMPILOT_AUTO_APPROVE` | Skip routine confirmations (use carefully) |
| `FORMPILOT_WEB_HOST` / `FORMPILOT_WEB_PORT` | Console bind |

Captcha vision is separate from the main model. Without `FORMPILOT_VISION_*`, FormPilot falls back to local `ddddocr`, then pauses for a human if needed.

---

## Agent tools (overview)

**Observe:** `inspect_page`, `inspect_widget`, `wait_and_rescan`, `get_profile_catalog`  
**Fill:** `fill_from_profile`, `fill_text`, `open_field`, `select_cascade_from_profile`, `set_date_from_profile`, `fill_family_from_profile`  
**Widgets:** `click_widget_option`, `click_widget_control`  
**Materials:** `list_local_documents`, `suggest_documents_for_requirement`, `merge_pdfs`, `upload_local_file`, `upload_materials_from_profile`  
**Human loop:** `request_user_confirmation`, `pause_for_user`, `request_missing_profile_fields`  
**Navigate:** `click_control`, `dismiss_page_overlays`, `attempt_auto_login`, `verify_field`

Supported controls include native inputs/selects, Element Plus / Ant Design Cascader & DatePicker patterns, and generic popup options. When a specialized adapter misses, the agent falls back to inspect → click → re-observe.

---

## Safety boundary

- Secret fields (password, SMS, file/signature) are policy-blocked from blind autofill  
- Login / save / submit / send-code clicks need one-shot approval  
- Approval tokens bind to URL + field + profile path and expire after use  
- No arbitrary JavaScript execution tool  

See [SECURITY.md](SECURITY.md) for the full policy surface.

---

## Project layout

```
formpilot/
  agent.py          # multi-step tool loop
  browser.py        # Playwright observe & act
  tools/            # model-facing tools + privacy filters
  policy.py         # approvals & hard blocks
  profile.py        # local vault + catalog
  documents.py      # PDF match / merge / upload helpers
  web/              # FastAPI console + static UI
  cli.py            # terminal entry
docs/ARCHITECTURE.md
tests_python/
examples/
```

---

## Tests

```bash
python3 -m unittest discover -s tests_python -v
python3 -m compileall -q formpilot
.venv/bin/python scripts/smoke_python_browser.py   # optional live browser
```

---

## Current limits

Cross-origin iframes, canvas-only widgets, and pure visual (coordinate) clicking are out of scope. Heavily customized site DOMs may need a human pause. Always respect site ToS and captcha rules.

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — module boundaries & data flow  
- [Contributing](CONTRIBUTING.md) — local development  
- [Security](SECURITY.md) — threat model & approvals  
