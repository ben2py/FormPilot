from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> bool:
    """Load a small dotenv-compatible file without adding a runtime dependency.

    Existing process variables win unless ``override`` is explicitly enabled.
    Supports comments, ``export KEY=value`` and single/double quoted values.
    """
    env_path = Path(path)
    if not env_path.exists():
        return False

    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"Invalid .env line {line_number}: expected KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"Invalid .env key on line {line_number}: {key!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if override or key not in os.environ:
            os.environ[key] = value
    return True


@dataclass(slots=True)
class AgentConfig:
    model: str = "gpt-5.6-terra"
    reasoning_effort: str = "medium"
    max_steps: int = 30
    headless: bool = False
    browser_profile_dir: Path = Path(".formpilot/browser-profile")
    cdp_url: str | None = None
    trace: bool = True

    @classmethod
    def from_env(cls) -> "AgentConfig":
        return cls(
            model=os.getenv("FORMPILOT_MODEL", "gpt-5.6-terra"),
            reasoning_effort=os.getenv("FORMPILOT_REASONING_EFFORT", "medium"),
            max_steps=int(os.getenv("FORMPILOT_MAX_STEPS", "30")),
            headless=os.getenv("FORMPILOT_HEADLESS", "0") == "1",
            browser_profile_dir=Path(os.getenv("FORMPILOT_BROWSER_PROFILE", ".formpilot/browser-profile")),
            cdp_url=os.getenv("FORMPILOT_CDP_URL") or None,
            trace=os.getenv("FORMPILOT_TRACE", "1") != "0",
        )
