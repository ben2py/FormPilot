from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any


_OCR = None
_CAPTCHA_PROMPT = (
    "这是一张网页登录图形验证码图片。请只识别图中的验证码字符。"
    "验证码通常是 4 位左右的英文字母或数字。"
    "只输出验证码本身（ASCII 字母/数字），不要解释、不要空格、不要标点。"
    "如果图片是空白、过小或无法辨认，只输出 EMPTY。"
)
_ASCII_CAPTCHA = re.compile(r"^[A-Za-z0-9]{3,8}$")


def _sanitize_captcha_text(text: str) -> str:
    """Keep ASCII letters/digits only. Unicode letters (e.g. Chinese) must not pass."""
    return "".join(
        ch
        for ch in str(text or "")
        if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("0" <= ch <= "9")
    )


def _extract_captcha_code(raw: str) -> str:
    text = str(raw or "").strip()
    if not text or text.upper() == "EMPTY":
        return ""
    compact = re.sub(r"\s+", "", text)
    # Prefer an explicit ASCII captcha token; ignore Chinese explanations.
    match = re.search(r"[A-Za-z0-9]{3,8}", compact)
    if match:
        candidate = _sanitize_captcha_text(match.group(0))
        if _ASCII_CAPTCHA.match(candidate):
            return candidate
    candidate = _sanitize_captcha_text(compact)
    if _ASCII_CAPTCHA.match(candidate):
        return candidate
    return ""


def png_looks_blank(png_bytes: bytes, *, min_bytes: int = 80) -> bool:
    """Heuristic: tiny or near-uniform PNG is useless for OCR/vision."""
    if not png_bytes or len(png_bytes) < min_bytes:
        return True
    try:
        from io import BytesIO

        from PIL import Image, ImageStat
    except ImportError:
        return False
    try:
        image = Image.open(BytesIO(png_bytes)).convert("L")
    except Exception:
        return True
    width, height = image.size
    if width < 20 or height < 12:
        return True
    if width * height < 400:
        return True
    stats = ImageStat.Stat(image)
    # Very low variance => solid/blank tile.
    return float(stats.var[0]) < 8.0


def maybe_save_captcha_debug(png_bytes: bytes, *, label: str = "captcha") -> str:
    """Persist last captcha crop for human debugging. Returns path or empty."""
    if not png_bytes:
        return ""
    try:
        directory = Path(".formpilot")
        directory.mkdir(parents=True, exist_ok=True)
        suffix = ".bin"
        if png_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            suffix = ".png"
        elif png_bytes[:2] == b"\xff\xd8":
            suffix = ".jpg"
        elif png_bytes[:6] in {b"GIF87a", b"GIF89a"}:
            suffix = ".gif"
        path = directory / f"{label}-debug{suffix}"
        path.write_bytes(png_bytes)
        # Keep a stable alias for quick open.
        alias = directory / f"{label}-debug.png"
        if suffix != ".png":
            try:
                from io import BytesIO

                from PIL import Image

                Image.open(BytesIO(png_bytes)).convert("RGB").save(alias, format="PNG")
            except Exception:
                alias.write_bytes(png_bytes)
        return str(alias if alias.exists() else path)
    except Exception:
        return ""


def _is_text_only_endpoint(base_url: str, model: str) -> bool:
    """Official DeepSeek chat endpoints reject image_url content parts."""
    haystack = f"{base_url} {model}".lower()
    return "deepseek" in haystack


def recognize_captcha_local(png_bytes: bytes) -> str:
    """OCR a captcha image with local ddddocr. Returns empty string on failure."""
    global _OCR
    if not png_bytes:
        return ""
    try:
        import ddddocr  # type: ignore
    except ImportError:
        return ""
    if _OCR is None:
        _OCR = ddddocr.DdddOcr(show_ad=False)
    text = _OCR.classification(png_bytes)
    return _extract_captcha_code(text)


def resolve_vision_settings(default_model: str | None = None) -> dict[str, str]:
    """Resolve dedicated vision credentials; do not reuse the main agent model by default.

    Env vars (preferred):
      FORMPILOT_VISION_MODEL
      FORMPILOT_VISION_BASE_URL
      FORMPILOT_VISION_API_KEY

    Fallbacks: OPENAI_BASE_URL / OPENAI_API_KEY / DASHSCOPE_API_KEY.
    FORMPILOT_MODEL is never used for captcha vision.
    """
    model = (default_model or os.getenv("FORMPILOT_VISION_MODEL") or "").strip()
    base_url = (os.getenv("FORMPILOT_VISION_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip()
    api_key = (
        os.getenv("FORMPILOT_VISION_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()
    return {"model": model, "base_url": base_url, "api_key": api_key}


def resolve_vision_model(default_model: str | None = None) -> str:
    return resolve_vision_settings(default_model)["model"]


async def recognize_captcha_with_llm(png_bytes: bytes, *, model: str | None = None) -> str:
    """Send captcha PNG to a vision-capable chat model. Never log the raw code."""
    if not png_bytes:
        return ""
    from openai import AsyncOpenAI

    settings = resolve_vision_settings(model)
    vision_model = settings["model"]
    if not vision_model:
        raise RuntimeError(
            "missing FORMPILOT_VISION_MODEL (e.g. qwen3-vl-plus); captcha vision uses a dedicated model"
        )
    if not settings["api_key"]:
        raise RuntimeError("missing FORMPILOT_VISION_API_KEY (or DASHSCOPE_API_KEY / OPENAI_API_KEY)")
    if _is_text_only_endpoint(settings["base_url"], vision_model):
        raise RuntimeError(
            "vision endpoint is text-only (DeepSeek); set FORMPILOT_VISION_BASE_URL to a VL API "
            "(e.g. https://dashscope.aliyuncs.com/compatible-mode/v1) and FORMPILOT_VISION_MODEL=qwen3-vl-plus"
        )

    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    client_kwargs: dict[str, Any] = {"api_key": settings["api_key"]}
    if settings["base_url"]:
        client_kwargs["base_url"] = settings["base_url"]
    client = AsyncOpenAI(**client_kwargs)
    create_kwargs: dict[str, Any] = {
        "model": vision_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": _CAPTCHA_PROMPT},
                ],
            }
        ],
        "max_tokens": 32,
    }
    response = await client.chat.completions.create(**create_kwargs)
    content = ""
    if response.choices:
        content = response.choices[0].message.content or ""
    return _extract_captcha_code(content)


async def recognize_captcha_image(png_bytes: bytes, *, model: str | None = None) -> dict[str, Any]:
    """Recognize captcha via local OCR, then LLM vision fallback.

    Returns ``{"text": "...", "backend": "ddddocr"|"llm"|"", "error": "..."}``.
    The text must not be forwarded to the agent tool transcript.
    """
    backend_pref = (os.getenv("FORMPILOT_CAPTCHA_BACKEND") or "auto").strip().lower()
    errors: list[str] = []
    debug_path = maybe_save_captcha_debug(png_bytes)
    if png_looks_blank(png_bytes):
        return {
            "text": "",
            "backend": "",
            "error": "captcha_image_blank",
            "debug_path": debug_path,
        }

    async def _llm() -> str:
        return await recognize_captcha_with_llm(png_bytes, model=model)

    if backend_pref in {"llm", "vision"}:
        try:
            text = await _llm()
            if text:
                return {"text": text, "backend": "llm", "error": "", "debug_path": debug_path}
            errors.append("llm_empty")
        except Exception as exc:
            errors.append(f"llm:{type(exc).__name__}")
        return {"text": "", "backend": "", "error": ";".join(errors) or "llm_failed", "debug_path": debug_path}

    if backend_pref in {"auto", "ddddocr", "local", ""}:
        try:
            text = recognize_captcha_local(png_bytes)
            if text and backend_pref != "auto":
                return {"text": text, "backend": "ddddocr", "error": "", "debug_path": debug_path}
            if text and backend_pref == "auto":
                return {"text": text, "backend": "ddddocr", "error": "", "debug_path": debug_path}
            if not text:
                errors.append("ddddocr_empty")
        except Exception as exc:
            errors.append(f"ddddocr:{type(exc).__name__}")

    if backend_pref in {"auto", "llm", "vision", ""}:
        try:
            text = await _llm()
            if text:
                return {"text": text, "backend": "llm", "error": "", "debug_path": debug_path}
            errors.append("llm_empty")
        except Exception as exc:
            errors.append(f"llm:{type(exc).__name__}")

    return {
        "text": "",
        "backend": "",
        "error": ";".join(errors) or "captcha_recognize_failed",
        "debug_path": debug_path,
    }
