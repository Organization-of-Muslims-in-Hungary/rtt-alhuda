"""Console logging for OpenRouter calls — never prints full API keys."""

from __future__ import annotations

import os
import time
from typing import Any

from rtt_alhuda.config import (
    OPENROUTER_API_URL,
    OPENROUTER_DEBUG,
    OPENROUTER_MODEL,
    OPENROUTER_TTS_MODEL,
    OPENROUTER_TTS_URL,
)


def _prefix() -> str:
    return f"[{time.strftime('%H:%M:%S')}] [OpenRouter]"


def mask_api_key(value: str | None) -> str:
    """Describe whether a key is set without exposing it."""

    if not value or not str(value).strip():
        return "(not set — add OPENROUTER_API_KEY to .env in repo root)"
    s = str(value).strip()
    if len(s) <= 8:
        return f"(set, length={len(s)})"
    return f"{s[:4]}…{s[-4:]} (len={len(s)})"


def info(msg: str, *extra: Any) -> None:
    print(_prefix(), msg, *extra, flush=True)


def debug(msg: str, *extra: Any) -> None:
    if OPENROUTER_DEBUG:
        print(_prefix(), "[debug]", msg, *extra, flush=True)


def warn(msg: str, *extra: Any) -> None:
    print(_prefix(), "WARN:", msg, *extra, flush=True)


def error(msg: str, *extra: Any) -> None:
    print(_prefix(), "ERROR:", msg, *extra, flush=True)


def log_startup_summary() -> None:
    """Log endpoints, models, and masked key once when the server starts."""

    key = os.getenv("OPENROUTER_API_KEY")
    info(
        "Startup — chat completions:",
        OPENROUTER_API_URL,
        "| model:",
        OPENROUTER_MODEL,
        "| OPENROUTER_API_KEY:",
        mask_api_key(key),
    )
    info(
        "Startup — TTS:",
        OPENROUTER_TTS_URL,
        "| model:",
        OPENROUTER_TTS_MODEL,
    )
    if OPENROUTER_DEBUG:
        info("OPENROUTER_DEBUG is on — logging each OpenRouter request/response (no secrets).")
    elif not key:
        error("OPENROUTER_API_KEY is missing — transcription and TTS will fail until it is set.")
