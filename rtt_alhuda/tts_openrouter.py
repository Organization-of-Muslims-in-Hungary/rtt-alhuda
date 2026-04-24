"""OpenRouter text-to-speech (POST /api/v1/tts)."""

import os
from typing import Literal

from aiohttp import ClientSession, ClientTimeout

from rtt_alhuda.config import (
    OPENROUTER_TTS_MODEL,
    OPENROUTER_TTS_RESPONSE_FORMAT,
    OPENROUTER_TTS_URL,
    voice_for_tts_language,
)

TtsLanguage = Literal["en", "hu"]


async def synthesize_speech_bytes(
    http: ClientSession,
    *,
    text: str,
    language: TtsLanguage,
    voice: str | None = None,
    timeout_s: float = 120.0,
) -> tuple[bytes, str | None]:
    """Call OpenRouter TTS; return raw audio body and optional X-Generation-Id."""

    if not text.strip():
        return b"", None

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY environment variable")

    voice_id = voice or voice_for_tts_language(language)
    payload: dict = {
        "input": text,
        "model": OPENROUTER_TTS_MODEL,
        "voice": voice_id,
    }
    if OPENROUTER_TTS_RESPONSE_FORMAT:
        payload["response_format"] = OPENROUTER_TTS_RESPONSE_FORMAT

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "rtt-alhuda-tts",
    }

    async with http.post(
        OPENROUTER_TTS_URL,
        json=payload,
        headers=headers,
        timeout=ClientTimeout(total=timeout_s),
    ) as resp:
        gen_id = resp.headers.get("X-Generation-Id")
        body = await resp.read()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"TTS error {resp.status}: {body[:500]!r}")

    return body, gen_id
