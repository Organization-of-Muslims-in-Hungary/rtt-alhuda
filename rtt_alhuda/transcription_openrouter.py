"""OpenRouter chat/completions API for live transcription."""

import json
import os

from aiohttp import ClientError, ClientSession, ClientTimeout

from rtt_alhuda.config import OPENROUTER_API_URL, OPENROUTER_MODEL
from rtt_alhuda import openrouter_debug as ord


async def send_chunk_to_openrouter(
    http: ClientSession,
    audio_b64_wav: str,
    original_ar: str,
    original_en: str,
    original_hu: str,
) -> dict:
    """Send one audio window to OpenRouter and return the parsed JSON result."""

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        ord.error("Refusing request: OPENROUTER_API_KEY is not set (check .env next to main.py).")
        raise RuntimeError("Missing OPENROUTER_API_KEY environment variable")

    ord.debug(
        "POST",
        OPENROUTER_API_URL,
        "| model:",
        OPENROUTER_MODEL,
        "| base64_wav_chars:",
        len(audio_b64_wav),
    )

    system = """
    You are a strict, verbatim audio-to-text transcriber and translator.

    You will receive:
    1. Context for what has already been transcribed and translated ("Context AR", "Context EN", "Context HU").
    2. An audio chunk (WAV) that OVERLAPS with the end of the context, and then continues with new audio.

    YOUR ONLY JOB: Transcribe and translate ONLY the NEW words spoken in the audio AFTER the provided context ends.

    The final JSON output MUST have keys "ar", "en", and "hu".
    ══ STRICT RULES — violating any rule is a critical failure ══

    RULE 1 — NO REPETITION:
    Carefully find where the "Context AR" text ends inside the audio.
    Output ONLY what comes after that point.
    If every word in the audio is already covered by the context, you MUST return empty strings for "ar", "en", and "hu".

    RULE 2 — NO HALLUCINATION FROM ELONGATION:
    Arabic recitation (Quran, Adhan, Khutbah) uses vocal elongation (مد). A long drawn-out
    vowel sound ("Allaaaaahu") is still ONE word (الله), NOT a signal to add more words.
    Do NOT use elongated sounds as a cue to predict or insert additional phrases.
    Transcribe only the discrete words that are clearly and completely spoken.

    RULE 3 — NO AUTOCOMPLETE:
    Do NOT finish incomplete sentences. Do NOT predict what will be said next.
    Do NOT use your knowledge of Adhan, Quran, or any formulaic phrases to insert text
    that was not clearly audible in this audio chunk.

    RULE 4 — SILENCE / NOISE:
    If the audio is silent, noisy, contains only elongated breath/vocal sounds with no
    new discrete words, or is too unclear, you MUST return empty strings for all fields.
    For example: {"ar": "", "en": "", "hu": ""}

    RULE 5 — INCOMPLETE LAST WORD:
    Do NOT include the last incomplete word/syllable at the end of the chunk.
    It will be sent again in the next chunk.

    RULE 6 — SCRIPT:
    The "ar" field MUST be written in Arabic script (Unicode Arabic letters: ا ب ت ...).
    NEVER romanize, transliterate, or write Arabic words using Latin letters.
    Example of WRONG output: "Bismillahirrahmanirrahim"
    Example of CORRECT output for the "ar" field: "بسم الله الرحمن الرحيم"

    RULE 7 — FORMAT:
    Return ONLY a valid JSON object. No explanations, no extra text, no newlines in values.
    """
    body = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.0,  # Force deterministic output
        "top_p": 0.1,  # Restrict token choices heavily
        "messages": [
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f'"Context AR": "{original_ar}", '
                            f'"Context EN": "{original_en}", '
                            f'"Context HU": "{original_hu}"'
                        ),
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": audio_b64_wav,
                            "format": "wav",
                        },
                    },
                ],
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "live_transcription_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "required": [
                        "ar",
                        "en",
                        "hu",
                    ],
                    "properties": {
                        "ar": {"type": "string"},
                        "en": {"type": "string"},
                        "hu": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "rtt-alhuda-node",
    }

    try:
        async with http.post(
            OPENROUTER_API_URL,
            json=body,
            headers=headers,
            timeout=ClientTimeout(total=120),
        ) as resp:
            raw_text = await resp.text()
            if resp.status < 200 or resp.status >= 300:
                snippet = raw_text[:1200] if raw_text else "(empty body)"
                ord.warn(
                    "chat/completions failed | HTTP",
                    resp.status,
                    "| body (truncated):",
                    snippet,
                )
                if resp.status in (401, 403):
                    ord.warn(
                        "Likely invalid or unauthorized API key — verify OPENROUTER_API_KEY at https://openrouter.ai/keys",
                    )
                raise RuntimeError(f"OpenRouter error ({resp.status}): {raw_text}")
            ord.debug("chat/completions OK | HTTP", resp.status, "| response_chars:", len(raw_text))
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                ord.error("chat/completions: response is not JSON |", repr(raw_text[:500]))
                raise RuntimeError(f"OpenRouter returned non-JSON: {exc}") from exc
    except ClientError as exc:
        ord.error("chat/completions network error:", repr(exc))
        raise RuntimeError(f"OpenRouter connection error: {exc}") from exc

    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "{}")
    if isinstance(content, str):
        return json.loads(content)

    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return json.loads("\n".join(pieces) if pieces else "{}")

    return {}
