from __future__ import annotations

import os


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def wait_scale() -> float:
    """Multiply browser sleeps. FORMPILOT_FAST defaults to 0.35x."""
    if env_flag("FORMPILOT_FAST"):
        raw = os.getenv("FORMPILOT_WAIT_SCALE", "0.35")
    else:
        raw = os.getenv("FORMPILOT_WAIT_SCALE", "1")
    try:
        scale = float(raw or "1")
    except ValueError:
        scale = 1.0
    return max(0.1, min(scale, 2.0))


def scaled_ms(milliseconds: int, *, minimum: int = 20, maximum: int = 5000) -> int:
    value = int(max(0, milliseconds) * wait_scale())
    return max(minimum, min(value, maximum))
