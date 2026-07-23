from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _parse_env_assignment(raw_line: str) -> tuple[str, str] | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not _ENV_KEY.fullmatch(key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    elif " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return key, value


def _quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch in value for ch in ' \t#"\'\\'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> bool:
    """Load a small dotenv-compatible file without adding a runtime dependency.

    Existing process variables win unless ``override`` is explicitly enabled.
    Supports comments, ``export KEY=value`` and single/double quoted values.
    """
    env_path = Path(path)
    if not env_path.exists():
        return False

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = _parse_env_assignment(raw_line)
        if parsed is None:
            stripped = raw_line.strip()
            if stripped and not stripped.startswith("#") and "=" not in stripped:
                raise ValueError(f"Invalid .env line {line_number}: expected KEY=value")
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value
    return True


def upsert_env_file(path: str | Path, updates: dict[str, str], *, apply: bool = True) -> Path:
    """Create or update KEY=value lines in a .env file while preserving other content."""
    env_path = Path(path)
    cleaned: dict[str, str] = {}
    for key, value in updates.items():
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"Invalid .env key: {key!r}")
        cleaned[key] = "" if value is None else str(value)

    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# Managed by FormPilot web console. Never commit real API keys.",
            "",
        ]

    seen: set[str] = set()
    out: list[str] = []
    for raw_line in lines:
        parsed = _parse_env_assignment(raw_line)
        if parsed is None:
            out.append(raw_line)
            continue
        key, _old = parsed
        if key in cleaned:
            out.append(f"{key}={_quote_env_value(cleaned[key])}")
            seen.add(key)
        else:
            out.append(raw_line)

    missing = [key for key in cleaned if key not in seen]
    if missing:
        if out and out[-1].strip():
            out.append("")
        for key in missing:
            out.append(f"{key}={_quote_env_value(cleaned[key])}")

    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(text, encoding="utf-8")

    if apply:
        for key, value in cleaned.items():
            os.environ[key] = value
    return env_path


@dataclass(slots=True)
class AgentConfig:
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    api_mode: str = "auto"
    max_steps: int = 80
    headless: bool = False
    browser_profile_dir: Path = Path(".formpilot/browser-profile")
    cdp_url: str | None = None
    trace: bool = True

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(
            model=os.getenv("FORMPILOT_MODEL", "gpt-5.6-terra"),
            reasoning_effort=os.getenv("FORMPILOT_REASONING_EFFORT", "medium"),
            api_mode=os.getenv("FORMPILOT_API_MODE", "auto"),
            max_steps=int(os.getenv("FORMPILOT_MAX_STEPS", "80")),
            headless=os.getenv("FORMPILOT_HEADLESS", "0") == "1",
            browser_profile_dir=Path(os.getenv("FORMPILOT_BROWSER_PROFILE", ".formpilot/browser-profile")),
            cdp_url=os.getenv("FORMPILOT_CDP_URL") or None,
            trace=os.getenv("FORMPILOT_TRACE", "1") != "0",
        )
