# Contributing

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
PLAYWRIGHT_BROWSERS_PATH=.formpilot/browsers playwright install chromium
cp .env.example .env
cp examples/profile.example.json profile.json
```

Never commit `.env`, `profile.json`, `.formpilot/`, browser profiles, screenshots containing personal data, or recorded sessions from real application sites.

## Checks

Run before every commit:

```bash
npm run check
.venv/bin/python scripts/smoke_python_browser.py
git diff --check
```

The smoke test is local-only and requires the Playwright Chromium download. CI runs syntax and unit tests but does not launch a browser.

## Change placement

- Python Agent behavior belongs under `formpilot/` with tests in `tests_python/`.
- Changes under `src/` apply only to the optional extension prototype.
- Tool schemas must use `additionalProperties: false` and keep all properties explicit.
- New side-effecting tools require an approval policy and denial/replay tests.
- Any change that sends additional page/profile data to the model requires a privacy test.

## Commit style

Use an imperative subject such as `Add dynamic select rescan tool`. Keep unrelated generated files and personal test data out of commits.
