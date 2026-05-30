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
    #     RULE 3 — NO HALLUCINATION FROM ELONGATION:
    # Arabic recitation (Quran, Adhan, Khutbah) uses vocal elongation (مد). A long drawn-out
    # vowel sound ("Allaaaaahu") is still ONE word (الله), NOT a signal to add more words.
    # Do NOT use elongated sounds as a cue to predict or insert additional phrases.
    # Transcribe only the discrete words that are clearly and completely spoken.


    system = """
        You are a strict, verbatim audio-to-text transcriber and translator.

    You will receive:
    1. "context_ar" — what has already been transcribed.
    2. "context_en" / "context_hu" — already-translated English and Hungarian.
    3. An audio chunk (WAV) that includes the original transcription then continues.

    YOUR JOB: Extract the text and reply with the diffs of those new words and their translation, so append only the NEW words that appear in the audio after "context_ar". 

    ══ STRICT RULES — violating any rule is a critical failure ══

    RULE 1 — NO REPETITION:
    The audio chunk will contain overlapped content, you should not output it twice.
    Carefully find where the context_ar ends inside the audio.
    Output ONLY what comes after that point.
    If every word in the audio is already covered by context_ar, return empty strings.
    Example:
    context_ar: "السلام عليكم"
    audio: "السلام عليكم ورحمة الله"
    output_ar: "ورحمة الله"

    
    RULE 2 — NO AUTOCOMPLETE:
    Audio may contain familiar incomplete sentences. Do NOT attempt to predict or complete them. Leave incomplete sentences as they are, without adding any words.
    Transcribe only what is clearly and fully spoken in the audio chunk, even if it results in incomplete phrases.
    Example:
    audio: "السلام عليكم"
    output_ar: "السلام عليكم" 
    
    Do NOT add "ورحمة الله" even if you know it is likely to come next (This is really a critical failure)


    RULE 3 — SILENCE / NOISE:
    If the audio is silent, noisy, contains only elongated breath/vocal sounds with no
    new discrete words, or is too unclear — return EXACTLY:
    {"ar": "", "en": "", "hu": ""}

    RULE 4 — INCOMPLETE LAST WORD:
    Do NOT include the last incomplete word/syllable at the end of the chunk.
    Incomplete audio words/syllables at the end of the chunk will be repeated in the next chunk, so do not incude if you had a bit of doubt about them being incomplete.
    Example:
    "audio: "السلام عليكم ورح"
    output_ar: "السلام عليكم"
    If the audio ends with "السلام عليكم ورح", do NOT output "ورحمة الله" — only output what you are sure is complete: "السلام عليكم" and omit the incomplete "ورح".

    RULE 5 — SCRIPT:
    "ar" MUST be written in Arabic script (Unicode Arabic letters: ا ب ت ...).
    Don't romanize, transliterate, or write Arabic words using Latin letters.
    Example of WRONG output: "Bismillahirrahmanirrahim"
    Example of CORRECT output: "بسم الله الرحمن الرحيم"

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
                        "text": json.dumps({
                            "context_ar": original_ar,
                            "context_en": original_en,
                            "context_hu": original_hu
                        }, ensure_ascii=False, indent=2),

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
