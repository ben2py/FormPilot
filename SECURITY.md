# Security Policy

FormPilot controls browsers and handles personal application data. Treat privacy regressions and approval bypasses as security issues.

## Supported version

Only the latest revision of the repository is currently supported.

## Reporting

Do not include real API keys, passwords, OTPs, identification numbers, browser profiles, cookies, or screenshots containing personal information in an issue or test fixture. Report the minimal reproduction privately to the repository owner.

## Security invariants

- `.env`, `profile.json`, `.formpilot/`, and browser state must remain Git-ignored.
- Page values and profile values must not be serialized into LLM context.
- Secret fields must remain blocked even if the model explicitly requests a fill.
- External side effects require a target-bound, single-use approval.
- The model must not receive arbitrary code execution in the page context.
- Cascader and date tools must remain bounded and stop on ambiguous options instead of choosing by position.

Run `python3 -m unittest discover -s tests_python -v` after security-related changes. The tests in `tests_python/test_agent.py` enforce the principal invariants.
