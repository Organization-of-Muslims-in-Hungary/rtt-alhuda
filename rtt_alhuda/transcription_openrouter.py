"""OpenRouter chat/completions API for live transcription."""

import json
import os

from aiohttp import ClientError, ClientSession, ClientTimeout

from rtt_alhuda.config import OPENROUTER_API_URL, OPENROUTER_MODEL
from rtt_alhuda import openrouter_debug as ord


async def send_chunk_to_openrouter(
    http: ClientSession,
    audio_b64_wav: str,
    original_transcription: str,
    original_translation: str,
    translation_language: str = "English",
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

    system = f"""
    You are a live transcriber and translator.
     
    You will be given an audio stream chunk and an existing transcription and translation. Transcribe and translate the new words
    in the audio with Arabic transcription (أ ب ت ...), and {translation_language} translation.
     
    YOUR JOB: append only the NEW words that have been said in the given audio.

    SUPER IMPORTANT: Do NOT add words that have NOT been said in the audio! And Do NOT repeat what is already in the original transcription/translation!. 
    ONLY respond with the new additional transcription and translation that is IN THE AUDIO and NOT included in the original transcription and translation. 
    If the the audio contains speech that is already written in the original transcription, DO NOT repeat it again in your response.

    SUPER IMPORTANT: You must be careful if the audio is silent, or unclear, or noisy (contains no speech) or just background noise, then you MUST return empty strings (exactly {{"new_additional_transcription": "", "new_additional_translation": ""}}) and nothing else! even if original sentence is not completed! (mind you that most of the times it will be empty audio!)

    Do NOT autocomplete! or hallucinate your own words or interpret unclear words! Only append what is actually spoken in the audio! 
    
    DO NOT include the last second (incomplete words/sentences) of the audio (they will be sent again in the next chunk with more context).
    And don't add new lines.
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
                            f'"original_transcription": "{original_transcription}", '
                            f'"original_translation": "{original_translation}"'
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
                        "new_additional_transcription",
                        "new_additional_translation",
                    ],
                    "properties": {
                        "new_additional_transcription": {"type": "string"},
                        "new_additional_translation": {"type": "string"},
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
