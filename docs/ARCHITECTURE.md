# Architecture

## Repository boundaries

`formpilot/` is the production implementation: a Python LLM agent using the OpenAI Responses API (or Chat Completions for DeepSeek-compatible providers) and Playwright. The repository intentionally contains no independent browser-extension implementation, so model orchestration, browser execution, safety policy, and tests share one versioned Python code path.

## Runtime flow

```text
CLI goal
  ↓
FormPilotAgent
  ↓ model tool choice (Responses or Chat Completions)
ToolRegistry
  ↓
FormPilotTools ── ApprovalPolicy
  ↓                    ↓
Playwright browser   terminal confirmation
  ↓
target form
```

The model never receives an arbitrary JavaScript execution tool. Browser operations are constrained to the schemas registered in `FormPilotTools.registry()`.

## State ownership

| State | Owner | Persisted |
|---|---|---|
| API configuration | `.env` / process environment | Local, Git-ignored |
| Personal form data | `profile.json` | Local, Git-ignored |
| Task brief | `task.md` | Local, usually Git-ignored |
| Run trajectory | `.formpilot/logs/run-*.jsonl` | Local, Git-ignored |
| Browser login state | `.formpilot/browser-profile` | Local, Git-ignored |
| LLM conversation | `FormPilotAgent.run()` | Memory for one run |
| One-time approvals | `ApprovalPolicy` | Memory, consumed once |
| Tool traces | Terminal | Not persisted by default |

## Privacy boundary

`ProfileStore.catalog()` exposes paths, labels, types, and risk classes—not values. `inspect_page` converts DOM values into `has_value` flags. `fill_from_profile` resolves the real value locally and removes it from the verification result before returning data to the model.

Changing any of these three boundaries requires a regression test proving that secret or personal values do not appear in serialized model context.

## Agent loop

`FormPilotAgent` preserves every model output item and appends each tool result as a matching `function_call_output`. The model gets a fresh decision after every tool round. Repeating an identical call more than three times returns a structured error, and the complete run stops at `max_steps`.

`create_model()` selects the transport: OpenAI Responses by default, or Chat Completions when `FORMPILOT_API_MODE=chat` / auto-detected DeepSeek. The Chat Completions adapter converts tool schemas, echoes DeepSeek `reasoning_content` across tool turns, and still feeds the agent the same `ToolCall` / `function_call_output` contract.

## Browser model

`PlaywrightFormBrowser` uses a persistent Chromium context by default. It attaches stable `data-formpilot-id` attributes to visible fields and controls, then exposes only those IDs to tools. All fill and click operations re-scan and require a unique live locator before acting.

Open custom widgets receive the same stable IDs. `WIDGET_SCAN_SCRIPT` recognizes ARIA listbox/tree/grid/dialog structures plus common Element Plus and Ant Design popovers. It returns widget roots, semantic options, nesting levels, calendar headers, and navigation controls.

`select_cascade` opens a field and resolves one local path component per visible menu level. It re-inspects after every choice and stops on missing or ambiguous options. `set_date` first handles native date inputs; otherwise it parses the open panel's year/month, uses bounded previous/next year/month controls, and selects a unique in-month date cell. Both operations have finite iteration limits and return structured failures rather than guessing.

The browser adapter can alternatively attach over CDP when `FORMPILOT_CDP_URL` is set.

## Safety model

- Password, CAPTCHA, OTP, file, and signature fields are blocked in code.
- Contact details, addresses, and document numbers require one-time authorization.
- Buttons with external side effects require one-time authorization.
- Approval tokens are bound to the current URL and exact field/control target.
- Ordinary next/previous navigation may proceed without confirmation.
- Webpage content is untrusted and cannot alter the system prompt or policy code.

## Testing layers

- `tests_python/`: Agent orchestration, privacy filtering, approval behavior, and fake-browser tests.
- `scripts/smoke_python_browser.py`: real Chromium scan/fill/dynamic-select test against `demo/`.

The smoke page contains a three-level non-native origin selector and a custom calendar initially positioned at September 2026. The test must select Zhejiang/Wenzhou/Rui'an and navigate to March 1, 2022 without using direct DOM value injection.
