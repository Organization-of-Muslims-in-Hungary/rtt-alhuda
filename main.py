"""Live transcription backend for the browser app.

The app runs a single aiohttp server that serves `index.html` and accepts a
WebSocket connection at `/stream`. When the browser sends `start`, the server
captures microphone audio locally, keeps a rolling PCM buffer, converts audio
windows to WAV, and sends them to OpenRouter for transcription and translation.

The code is organized around three responsibilities:
- capturing microphone input,
- chunking and processing buffered audio,
- returning transcription updates back to the browser in real time.

(TODO: in production, make it work headlessly with no frontend)
"""

import asyncio
import base64
import json
import os
import platform
import sys
import time
import traceback
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

# Work around a Windows + Python 3.14 issue where platform.system()
# can block inside a WMI query and make aiohttp import appear stuck.
if os.name == "nt" and sys.version_info >= (3, 14):
    platform.system = lambda: "Windows"

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
import aiohttp_cors
import edge_tts
from dotenv import load_dotenv
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer
import fractions

import webrtcvad

vad = webrtcvad.Vad(2)  # aggressiveness 0-3 (2 is aggressive)

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
PROCESSING_INTERVAL_SECONDS = 3
CONTEXT_CHUNK_COUNT = 2  # Number of past chunks to include in the payload for context
SAMPLE_RATE = 44100  # USB mic native rate
VAD_RATE = 16000     # WebRTC VAD requires 8k/16k/32k/48k
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
FRAME_CHUNK_SECONDS = 0.1
MAX_BUFFER_SECONDS = 120  # Increased to prevent dropping audio during API delays


def _downsample_to_16k(pcm_data: bytes, from_rate: int = SAMPLE_RATE) -> bytes:
    """Simple integer-ratio downsample from 44100 to 16000 (via linear interp)."""
    import struct as _struct
    samples = _struct.unpack(f"<{len(pcm_data)//2}h", pcm_data)
    ratio = from_rate / VAD_RATE
    out_len = int(len(samples) / ratio)
    out = []
    for i in range(out_len):
        src = i * ratio
        idx = int(src)
        if idx >= len(samples) - 1:
            out.append(samples[-1])
        else:
            frac = src - idx
            out.append(int(samples[idx] * (1 - frac) + samples[idx + 1] * frac))
    return _struct.pack(f"<{len(out)}h", *out)


def is_speech_present(pcm_data: bytes) -> bool:
    """Check if a PCM audio chunk contains speech using WebRTC VAD."""
    try:
        # Downsample to 16kHz for VAD (which only supports 8k/16k/32k/48k)
        pcm_16k = _downsample_to_16k(pcm_data)
        # WebRTC VAD requires exact frame durations: 10, 20, or 30 ms.
        frame_duration_ms = 30
        frame_bytes = int((VAD_RATE * frame_duration_ms / 1000.0) * CHANNELS * SAMPLE_WIDTH_BYTES)
        
        speech_frames = 0
        total_frames = 0
        
        for i in range(0, len(pcm_16k) - frame_bytes + 1, frame_bytes):
            frame = pcm_16k[i:i+frame_bytes]
            if vad.is_speech(frame, VAD_RATE):
                speech_frames += 1
            total_frames += 1
            
        # If less than ~5% of frames contain speech, we consider it silent noise.
        speech_ratio = speech_frames / total_frames if total_frames > 0 else 0
        return speech_ratio >= 0.05
    except Exception as e:
        print(f"VAD error: {e}")
        return True  # Fallback to true so we don't drop audio falsely


# =========================================================================
# TTS Configuration — OpenRouter TTS with edge-tts fallback
# Voices: alloy, echo, fable, onyx, nova, shimmer
# =========================================================================
OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/tts"
OPENROUTER_TTS_MODEL = os.getenv("OPENROUTER_TTS_MODEL", "openai/tts-1")
OPENROUTER_TTS_VOICE_EN = os.getenv("OPENROUTER_TTS_VOICE_EN", "onyx")
OPENROUTER_TTS_VOICE_HU = os.getenv("OPENROUTER_TTS_VOICE_HU", "onyx")
# edge-tts fallback voices (used when OpenRouter TTS fails)
EDGE_TTS_EN_VOICE = "en-GB-RyanNeural"
EDGE_TTS_HU_VOICE = "hu-HU-TamasNeural"


async def _synthesize_edge_tts_fallback(text: str, voice: str) -> bytes | None:
    """Fallback TTS using edge-tts when OpenRouter TTS is unavailable."""
    try:
        communicate = edge_tts.Communicate(text, voice)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks) if chunks else None
    except Exception as e:
        log(f"edge-tts fallback error ({voice}): {e}")
        return None


async def synthesize_openrouter_tts(text: str, voice: str, http: ClientSession) -> bytes | None:
    """Generate MP3 audio via OpenRouter TTS, with edge-tts fallback.

    Tries OpenRouter first (openai/tts-1). If it fails for any reason,
    automatically falls back to edge-tts so audio always works.
    """
    if not text or not text.strip():
        return None

    api_key = os.getenv("OPENROUTER_API_KEY")
    if api_key:
        payload = {
            "input": text,
            "model": OPENROUTER_TTS_MODEL,
            "voice": voice,
            "response_format": "mp3",
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:5173",
            "X-Title": "rtt-alhuda",
        }
        try:
            async with http.post(
                OPENROUTER_TTS_URL,
                json=payload,
                headers=headers,
                timeout=ClientTimeout(total=60),
            ) as resp:
                body = await resp.read()
                if resp.status == 200:
                    return body
                log(f"OpenRouter TTS error {resp.status}: {body[:200]!r} — falling back to edge-tts")
        except Exception as e:
            log(f"OpenRouter TTS failed ({e}) — falling back to edge-tts")

    # Fallback: edge-tts (always works, no key needed)
    edge_voice = EDGE_TTS_EN_VOICE if "alloy" in voice or "onyx" in voice or "echo" in voice else EDGE_TTS_HU_VOICE
    # Map based on context: if voice matches EN config use EN voice else HU
    if voice == OPENROUTER_TTS_VOICE_EN:
        edge_voice = EDGE_TTS_EN_VOICE
    elif voice == OPENROUTER_TTS_VOICE_HU:
        edge_voice = EDGE_TTS_HU_VOICE
    return await _synthesize_edge_tts_fallback(text, edge_voice)


def _remove_leading_overlap(new_text: str, existing_text: str, min_words: int = 3) -> str:
    """Remove any prefix of new_text that duplicates a tail of existing_text."""
    if not new_text or not existing_text:
        return new_text
    new_words = new_text.split()
    exist_words = existing_text.split()
    if not new_words or not exist_words:
        return new_text
    max_check = min(len(new_words), len(exist_words), 20)
    for window in range(max_check, min_words - 1, -1):
        if exist_words[-window:] == new_words[:window]:
            return " ".join(new_words[window:])
    return new_text


# A 3-second chunk at normal Arabic speech speed rarely exceeds 15 new words
_MAX_NEW_WORDS_PER_CHUNK = 15       # Arabic audio chunks (hallucination prone)
_MAX_TRANSLATION_WORDS = 60         # Translations — far more words per chunk; only repetition-guard needed


def _sanitize_output(text: str, word_cap: int = _MAX_NEW_WORDS_PER_CHUNK) -> str:
    """Truncate hallucinated / repetitive phrases from a single chunk.

    Uses a proper sliding-window n-gram counter so interleaved patterns like
    'Allahu Akbar Allahu Akbar Allahu Akbar' are caught reliably.

    Rules (applied in order, first match wins):
      - word_cap: hard limit per chunk (15 for Arabic, 60 for translations)
      - n=2, max 2 occurrences: 'Allahu Akbar' is legitimately said twice in Adhan;
        a 3rd occurrence in one chunk is hallucination → cut before it
      - n=1, max 3 occurrences: any single word appearing 4+ times triggers cut
    """
    if not text:
        return text

    words = text.split()

    # 1. Hard word cap
    if len(words) > word_cap:
        words = words[:word_cap]

    # 2. Sliding-window n-gram repeat detector
    for n, max_count in [(2, 2), (1, 3)]:
        if len(words) < n:
            continue
        counts: dict = {}
        cut_at = None
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            counts[gram] = counts.get(gram, 0) + 1
            if counts[gram] > max_count:
                cut_at = i
                break
        if cut_at is not None:
            words = words[:cut_at]
            break

    return " ".join(words)


def _split_text_for_tts(text: str, max_chars: int = 500) -> list[str]:
    """Split text into segments at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    segments = []
    current = ""
    for sentence in text.replace(". ", ".\n").split("\n"):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(current) + len(sentence) + 1 > max_chars and current:
            segments.append(current.strip())
            current = sentence
        else:
            current = current + " " + sentence if current else sentence
    if current.strip():
        segments.append(current.strip())
    return segments if segments else [text]


@dataclass
class ChunkInfo:
    """Stores exact sample boundaries and results for a transcribed audio chunk."""
    start_sample: int
    end_sample: int
    transcription: str
    translation: str
    translation_hu: str = ""


# Global set of SSE queues — one per connected frontend client
_sse_listeners: set[asyncio.Queue] = set()       # text stream
_audio_listeners: set[asyncio.Queue] = set()     # audio stream (TV page)
_active_pcs: set = set()  # Track open RTCPeerConnections

# Global recording state — allows the /control API to track status
_recording_state: dict = {"active": False, "started_at": None}
_ws_clients: list = []  # All active WebSocket ClientState instances
_all_ws_browsers: list = []  # Every connected browser WebSocket (recording or idle)
_active_recording_client: "ClientState | None" = None  # The client currently recording


class _NullWebSocket:
    """Drop-in replacement for web.WebSocketResponse when there is no browser.

    Used by the headless ClientState so the control API can start/stop
    recording without requiring an operator browser tab to be open.
    """
    closed = True  # send_log skips sending when ws.closed is True

    async def send_str(self, _data: str) -> None:  # noqa: D401
        pass

    async def close(self) -> None:
        pass


# Persistent headless client — used by the /api/control/* REST API.
# Exists for the lifetime of the server; no browser tab needed.
_headless_client: "ClientState | None" = None  # set in on_startup

# ── Screen registry: one entry per connected TV / monitor ──────────────────
@dataclass
class ScreenClient:
    """A connected display screen (TV, monitor, projector)."""
    screen_id: str               # 4-digit random ID shown on screen
    ws: web.WebSocketResponse
    page: str                    # "tv" | "screen" | "screen/ar" | "screen/en" | "screen/hu"
    lang: Optional[str] = None  # current language filter (None = all)

_screen_clients: dict[str, ScreenClient] = {}  # screen_id → ScreenClient


async def _broadcast_translations(
    ar: str, en: str, hu: str,
    new_ar: str = "", new_en: str = "", new_hu: str = ""
) -> None:
    """Push translations to all SSE text listeners.

    ``ar/en/hu`` = full accumulated text (kept for React compatibility).
    ``new_ar/new_en/new_hu`` = only the latest chunk — display pages use these
    to avoid the same text re-appearing with every new chunk.
    """
    payload = json.dumps({
        "ar": ar, "en": en, "hu": hu,
        "new_ar": new_ar, "new_en": new_en, "new_hu": new_hu,
    })
    dead = set()
    for q in _sse_listeners:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_listeners.difference_update(dead)


async def _broadcast_audio(lang: str, mp3_b64: str) -> None:
    """Push a TTS audio chunk to all SSE audio listeners (TV page)."""
    payload = json.dumps({"lang": lang, "audio": mp3_b64})
    dead = set()
    for q in _audio_listeners:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _audio_listeners.difference_update(dead)


@dataclass
class ClientState:
    """Per-WebSocket runtime state for one connected browser session."""

    ws: web.WebSocketResponse
    pcm_buffer: bytearray = field(default_factory=bytearray)
    buffer_start_sample: int = 0
    total_samples_written: int = 0
    chunk_history: list[ChunkInfo] = field(default_factory=list)
    recorder_task: Optional[asyncio.Task] = None
    processor_task: Optional[asyncio.Task] = None
    recording: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_chunk_end_sample: int = 0


def get_hours_timestamp() -> str:
    """Return a human-readable timestamp used in server logs."""

    return time.strftime("%H:%M:%S")


def log(*parts: object) -> None:
    """Print a timestamped log line to stdout."""

    print(f"[{get_hours_timestamp()}]", *parts, flush=True)


async def send_log(client: ClientState, message: str, level: str = "info") -> None:
    """Send a structured log message to the connected browser."""

    payload = {"type": "log", "level": level, "message": message}
    if not client.ws.closed:
        await client.ws.send_str(json.dumps(payload))


def create_wav_bytes(pcm_data: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw PCM bytes in a WAV container for OpenRouter audio input."""

    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return wav_buffer.getvalue()


async def capture_microphone_loop(client: ClientState) -> None:
    """Continuously read microphone audio into the client's rolling buffer."""

    try:
        import sounddevice as sd
    except ImportError:
        await send_log(
            client,
            "Missing dependency: sounddevice. Install with: pip install sounddevice",
            "error",
        )
        client.recording = False
        return

    frame_chunk = int(SAMPLE_RATE * FRAME_CHUNK_SECONDS)
    max_buffer_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=frame_chunk,
        ) as stream:
            await send_log(client, "Microphone capture started")
            while client.recording:  # use only client.recording — not ws.closed (headless client has ws.closed=True)
                data, overflowed = await asyncio.to_thread(stream.read, frame_chunk)
                if overflowed:
                    await send_log(client, "Input overflow detected", "warn")

                pcm_bytes = bytes(data)
                sample_count = len(pcm_bytes) // bytes_per_frame
                async with client.lock:
                    client.pcm_buffer.extend(pcm_bytes)
                    client.total_samples_written += sample_count

                    available_samples = (
                        client.total_samples_written - client.buffer_start_sample
                    )
                    overflow_samples = available_samples - max_buffer_samples
                    if overflow_samples > 0:
                        overflow_bytes = overflow_samples * bytes_per_frame
                        del client.pcm_buffer[:overflow_bytes]
                        client.buffer_start_sample += overflow_samples
    except Exception as exc:
        await send_log(client, f"Microphone error: {exc}", "error")
        client.recording = False
    finally:
        client.recording = False
        await send_log(client, "Microphone capture stopped")


async def send_chunk_to_openrouter(
    http: ClientSession,
    audio_b64_wav: str,
    original_transcription: str,
    original_translation: str,
    original_translation_hu: str = "",
) -> dict:
    """Send one audio window to OpenRouter and return the parsed JSON result."""

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY environment variable")

    system = """
    You are a strict, verbatim audio-to-text transcriber and translator.

    You will receive:
    1. "original_transcription" — what has already been transcribed.
    2. "original_translation" / "original_translation_hu" — already-translated English and Hungarian.
    3. An audio chunk (WAV) that OVERLAPS with the end of the original transcription then continues.

    YOUR ONLY JOB: output the NEW words spoken AFTER the original transcription ends. Nothing else.

    ══ STRICT RULES — violating any rule is a critical failure ══

    RULE 1 — NO REPETITION:
    Carefully find where the original_transcription ends inside the audio.
    Output ONLY what comes after that point.
    If every word in the audio is already covered by original_transcription, return empty strings.

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
    new discrete words, or is too unclear — return EXACTLY:
    {"new_additional_transcription": "", "new_additional_translation": "", "new_additional_translation_hu": ""}

    RULE 5 — INCOMPLETE LAST WORD:
    Do NOT include the last incomplete word/syllable at the end of the chunk.
    It will be sent again in the next chunk.

    RULE 6 — SCRIPT:
    "new_additional_transcription" MUST be written in Arabic script (Unicode Arabic letters: ا ب ت ...).
    NEVER romanize, transliterate, or write Arabic words using Latin letters.
    Example of WRONG output: "Bismillahirrahmanirrahim"
    Example of CORRECT output: "بسم الله الرحمن الرحيم"

    RULE 7 — FORMAT:
    Return ONLY a valid JSON object. No explanations, no extra text, no newlines in values.
    """
    body = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.0,       # Force deterministic output
        "top_p": 0.1,             # Restrict token choices heavily
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
                            f'"original_translation": "{original_translation}", '
                            f'"original_translation_hu": "{original_translation_hu}"'
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
                        "new_additional_translation_hu",
                    ],
                    "properties": {
                        "new_additional_transcription": {"type": "string"},
                        "new_additional_translation": {"type": "string"},
                        "new_additional_translation_hu": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            },
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5173",
    }
    async with http.post(
        OPENROUTER_API_URL,
        json=body,
        headers=headers,
        timeout=ClientTimeout(total=30),
    ) as resp:
        raw_text = await resp.text()
        if resp.status < 200 or resp.status >= 300:
            raise RuntimeError(f"OpenRouter error ({resp.status}): {raw_text}")
        payload = json.loads(raw_text)

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


async def _generate_and_send_tts(
    client: ClientState,
    http: ClientSession,
    new_translation: str,
    new_translation_hu: str,
) -> None:
    """Generate TTS and push it to clients without blocking main text loop."""
    tts_en_bytes, tts_hu_bytes = await asyncio.gather(
        synthesize_openrouter_tts(new_translation, OPENROUTER_TTS_VOICE_EN, http) if new_translation.strip() else asyncio.sleep(0, result=None),
        synthesize_openrouter_tts(new_translation_hu, OPENROUTER_TTS_VOICE_HU, http) if new_translation_hu.strip() else asyncio.sleep(0, result=None),
    )

    if not tts_en_bytes and not tts_hu_bytes:
        return

    # Broadcast audio to TV page SSE listeners
    if tts_en_bytes:
        await _broadcast_audio("en", base64.b64encode(tts_en_bytes).decode("ascii"))
    if tts_hu_bytes:
        await _broadcast_audio("hu", base64.b64encode(tts_hu_bytes).decode("ascii"))

    # Push to legacy WebSocket UI
    message = {"type": "tts_audio"}
    if tts_en_bytes:
        message["ttsEnAudio"] = base64.b64encode(tts_en_bytes).decode("ascii")
    if tts_hu_bytes:
        message["ttsHuAudio"] = base64.b64encode(tts_hu_bytes).decode("ascii")

    if not client.ws.closed:
        await client.ws.send_str(json.dumps(message))
    else:
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(json.dumps(message))


async def _process_chunk(
    client: ClientState,
    http: ClientSession,
    wav_b64: str,
    new_audio_start_sample: int,
    new_audio_end_sample: int,
    original_transcription: str,
    original_translation: str,
    original_translation_hu: str,
    chunk_duration_seconds: float,
):
    try:
        start_time = time.time()
        result = await send_chunk_to_openrouter(
            http,
            wav_b64,
            original_transcription,
            original_translation,
            original_translation_hu,
        )
        latency_ms = int((time.time() - start_time) * 1000)

        new_transcription = str(result.get("new_additional_transcription", ""))
        new_translation = str(result.get("new_additional_translation", ""))
        new_translation_hu = str(result.get("new_additional_translation_hu", ""))

        # Post-process: strip any leading phrases already present in context
        # (catches hallucinated repetitions caused by Adhan elongation, etc.)
        new_transcription = _remove_leading_overlap(new_transcription, original_transcription)
        new_translation = _remove_leading_overlap(new_translation, original_translation)
        new_translation_hu = _remove_leading_overlap(new_translation_hu, original_translation_hu)

        # Hard safety net: word cap + consecutive repetition truncation
        # Arabic keeps a tight cap (15 words) to suppress audio hallucinations.
        # Translations use a generous cap (60 words) — Arabic is far more concise
        # than English/Hungarian, so 15 Arabic words can easily become 30+ in translation.
        new_transcription = _sanitize_output(new_transcription, word_cap=_MAX_NEW_WORDS_PER_CHUNK)
        new_translation = _sanitize_output(new_translation, word_cap=_MAX_TRANSLATION_WORDS)
        new_translation_hu = _sanitize_output(new_translation_hu, word_cap=_MAX_TRANSLATION_WORDS)

        # Fire off TTS generation in the background so it doesn't block WS message
        if new_translation.strip() or new_translation_hu.strip():
            asyncio.create_task(
                _generate_and_send_tts(client, http, new_translation, new_translation_hu)
            )

        async with client.lock:
            client.chunk_history.append(
                ChunkInfo(
                    start_sample=new_audio_start_sample,
                    end_sample=new_audio_end_sample,
                    transcription=new_transcription,
                    translation=new_translation,
                    translation_hu=new_translation_hu,
                )
            )

        # Broadcast to React SSE clients whenever we have new text
        if new_transcription or new_translation or new_translation_hu:
            async with client.lock:
                full_ar = " ".join(c.transcription for c in client.chunk_history if c.transcription)
                full_en = " ".join(c.translation for c in client.chunk_history if c.translation)
                full_hu = " ".join(c.translation_hu for c in client.chunk_history if c.translation_hu)
            await _broadcast_translations(
                full_ar, full_en, full_hu,
                new_ar=new_transcription,
                new_en=new_translation,
                new_hu=new_translation_hu,
            )

        message = {
            "type": "transcription",
            "transcription": new_transcription,
            "translation": new_translation,
            "translationHu": new_translation_hu,
            "originalTranscription": original_transcription,
            "originalTranslation": original_translation,
            "originalTranslationHu": original_translation_hu,
            "rawResponse": json.dumps(result),
            "originalAudioChunk": wav_b64,
            "processedChunks": 1,
            "windowSeconds": chunk_duration_seconds,
            "chunkDurationSeconds": chunk_duration_seconds,
            "latencyMs": latency_ms,
        }

        if not client.ws.closed:
            await client.ws.send_str(json.dumps(message))
        else:
            # Headless client — broadcast to all open browser tabs
            for c in _all_ws_browsers:
                if not c.ws.closed:
                    await c.ws.send_str(json.dumps(message))

    except (asyncio.TimeoutError, ClientError, RuntimeError, ValueError) as exc:
        await send_log(client, f"Error processing chunk ({type(exc).__name__}): {exc}", "error")
    except Exception as exc:
        await send_log(
            client,
            f"Unexpected error processing chunk ({type(exc).__name__}): {exc}\n{traceback.format_exc()}",
            "error",
        )


async def process_audio_loop(client: ClientState, http: ClientSession) -> None:
    """Periodically convert buffered audio into transcript and translation updates."""

    await send_log(
        client,
        f"Audio processing started (every {PROCESSING_INTERVAL_SECONDS}s, dynamic window)",
    )

    try:
        next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS

        while client.recording:  # use only client.recording — not ws.closed (headless client has ws.closed=True)
            wait_seconds = next_cycle_at - time.monotonic()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

            async with client.lock:
                end_sample = client.total_samples_written
                chunk_pcm = b""
                new_audio_pcm = b""
                total_samples = 0
                
                if CONTEXT_CHUNK_COUNT > 0:
                    history_to_include = client.chunk_history[-CONTEXT_CHUNK_COUNT:]
                else:
                    history_to_include = []
                
                if history_to_include:
                    past_start_sample = history_to_include[0].start_sample
                    original_transcription = " ".join(c.transcription for c in history_to_include if c.transcription).strip()
                    original_translation = " ".join(c.translation for c in history_to_include if c.translation).strip()
                    original_translation_hu = " ".join(c.translation_hu for c in history_to_include if c.translation_hu).strip()
                else:
                    past_start_sample = client.last_chunk_end_sample
                    original_transcription = ""
                    original_translation = ""
                    original_translation_hu = ""

                new_audio_start_sample = client.last_chunk_end_sample
                new_audio_end_sample = end_sample

                new_samples_count = new_audio_end_sample - new_audio_start_sample
                
                # Check if we have enough new audio, otherwise wait
                if new_samples_count < int(SAMPLE_RATE * PROCESSING_INTERVAL_SECONDS):
                    pass
                else:
                    start_sample = max(client.buffer_start_sample, past_start_sample)
                    start_offset_samples = start_sample - client.buffer_start_sample
                    end_offset_samples = end_sample - client.buffer_start_sample
                    
                    start_byte = start_offset_samples * bytes_per_frame
                    end_byte = end_offset_samples * bytes_per_frame
                    chunk_pcm = bytes(client.pcm_buffer[start_byte:end_byte])
                    
                    # For VAD, we only want to check the NEW audio, not the historical overlap
                    safe_new_audio_start = max(client.buffer_start_sample, new_audio_start_sample)
                    new_start_offset = safe_new_audio_start - client.buffer_start_sample
                    new_start_byte = new_start_offset * bytes_per_frame
                    new_audio_pcm = bytes(client.pcm_buffer[new_start_byte:end_byte])
                    
                    total_samples = end_sample - start_sample

            if not chunk_pcm:
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue

            # --- WebRTC VAD check on exclusively new audio
            if not is_speech_present(new_audio_pcm):
                await send_log(client, "Silent audio chunk detected by VAD, skipping LLM request.")
                async with client.lock:
                    client.last_chunk_end_sample = new_audio_end_sample
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue
            # ------------------------

            wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
            wav_b64 = base64.b64encode(wav_bytes).decode("ascii")

            request_started_at = time.monotonic()

            await _process_chunk(
                client,
                http,
                wav_b64,
                new_audio_start_sample,
                new_audio_end_sample,
                original_transcription,
                original_translation,
                original_translation_hu,
                chunk_duration_seconds=total_samples / SAMPLE_RATE,
            )

            async with client.lock:
                client.last_chunk_end_sample = new_audio_end_sample

            # Keep a request-start-based cadence: every PROCESSING_INTERVAL_SECONDS
            # from request start, or immediately after slower responses.
            next_cycle_at = request_started_at + PROCESSING_INTERVAL_SECONDS

    finally:
        await send_log(client, "Audio processing stopped")


async def stop_recording(client: ClientState) -> None:
    """Stop the active recording and cancel background tasks for the client."""
    global _active_recording_client

    client.recording = False

    tasks = [task for task in (client.recorder_task, client.processor_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    client.recorder_task = None
    client.processor_task = None
    _recording_state["active"] = False
    _recording_state["started_at"] = None
    _active_recording_client = None
    if client in _ws_clients:
        _ws_clients.remove(client)


async def start_recording(client: ClientState, http: ClientSession) -> None:
    """Reset client state and start microphone capture plus audio processing."""
    global _active_recording_client

    if client.recording:
        await send_log(client, "Recording already running", "warn")
        return

    client.recording = True
    client.pcm_buffer.clear()
    client.buffer_start_sample = 0
    client.total_samples_written = 0
    client.chunk_history.clear()
    client.last_chunk_end_sample = 0

    client.recorder_task = asyncio.create_task(capture_microphone_loop(client))
    client.processor_task = asyncio.create_task(process_audio_loop(client, http))
    await send_log(client, "Recording started")
    _recording_state["active"] = True
    _recording_state["started_at"] = time.time()
    _active_recording_client = client
    _ws_clients.append(client)


async def generate_final_report(client: ClientState, http: ClientSession) -> None:
    """Generate final khutbah report: text files + full TTS audio files."""
    await send_log(client, "Generating final report...")

    async with client.lock:
        history = list(client.chunk_history)

    if not history:
        await send_log(client, "No chunks to generate report from", "warn")
        return

    # Combine all text
    full_arabic = " ".join(c.transcription for c in history if c.transcription).strip()
    full_english = " ".join(c.translation for c in history if c.translation).strip()
    full_hungarian = " ".join(c.translation_hu for c in history if c.translation_hu).strip()

    # Create reports directory
    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    prefix = f"khutbah_{timestamp}"

    # Write text files immediately
    files_created = []

    arabic_path = reports_dir / f"{prefix}_arabic.txt"
    arabic_path.write_text(full_arabic, encoding="utf-8")
    files_created.append(("Arabic Text", f"/reports/{arabic_path.name}"))
    await send_log(client, f"Saved: {arabic_path.name}")

    english_path = reports_dir / f"{prefix}_english.txt"
    english_path.write_text(full_english, encoding="utf-8")
    files_created.append(("English Translation", f"/reports/{english_path.name}"))
    await send_log(client, f"Saved: {english_path.name}")

    hungarian_path = reports_dir / f"{prefix}_hungarian.txt"
    hungarian_path.write_text(full_hungarian, encoding="utf-8")
    files_created.append(("Hungarian Translation", f"/reports/{hungarian_path.name}"))
    await send_log(client, f"Saved: {hungarian_path.name}")

    # Send text file links immediately so user can download right away
    text_msg = {
        "type": "report_ready",
        "files": [{"label": label, "url": url} for label, url in files_created],
        "timestamp": timestamp,
    }
    if not client.ws.closed:
        await client.ws.send_str(json.dumps(text_msg))
    else:
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(json.dumps(text_msg))

    # Generate full TTS audio files (may take time for long text)
    try:
        await send_log(client, "Generating audio files (this may take a minute)...")
        for lang_text, voice, suffix in [
            (full_english, OPENROUTER_TTS_VOICE_EN, "english"),
            (full_hungarian, OPENROUTER_TTS_VOICE_HU, "hungarian"),
        ]:
            if not lang_text:
                continue
            audio_data = await synthesize_openrouter_tts(lang_text, voice, http)
            if audio_data:
                audio_path = reports_dir / f"{prefix}_{suffix}.mp3"
                audio_path.write_bytes(audio_data)
                files_created.append((f"{suffix.title()} Audio", f"/reports/{audio_path.name}"))
                await send_log(client, f"Saved: {audio_path.name}")
    except Exception as e:
        await send_log(client, f"Audio generation error: {e}", "error")
        log(f"TTS report error: {e}")

    # Send final report with all files (text + audio)
    report_msg = {
        "type": "report_ready",
        "files": [{"label": label, "url": url} for label, url in files_created],
        "timestamp": timestamp,
    }
    if not client.ws.closed:
        await client.ws.send_str(json.dumps(report_msg))
    else:
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(json.dumps(report_msg))

    await send_log(client, f"Report complete! {len(files_created)} files generated.")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle browser control messages for a single WebSocket session."""

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    http: ClientSession = request.app["http_client"]
    client = ClientState(ws=ws)
    _all_ws_browsers.append(client)

    await send_log(client, "WebSocket connected")
    log("WebSocket client connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send_log(client, "Invalid JSON message", "warn")
                    continue

                msg_type = payload.get("type")
                if msg_type == "start":
                    await start_recording(client, http)
                elif msg_type == "stop":
                    await stop_recording(client)
                    await send_log(client, "Recording stopped")
                elif msg_type == "generate_report":
                    await generate_final_report(client, http)
                else:
                    await send_log(client, f"Unknown message type: {msg_type}", "warn")
            elif msg.type == WSMsgType.ERROR:
                await send_log(client, f"WebSocket error: {ws.exception()}", "error")
    finally:
        # Only stop recording if this client is currently the active one.
        # If the phone started recording via a different client, don't cancel it.
        if client is _active_recording_client:
            await stop_recording(client)
        elif client.recording:
            # This client was recording independently — clean it up
            await stop_recording(client)
        if client in _all_ws_browsers:
            _all_ws_browsers.remove(client)
        log("WebSocket client disconnected")

    return ws


async def index_handler(_: web.Request) -> web.StreamResponse:
    """Serve the browser UI from templates/index.html."""

    index_path = Path(__file__).parent / "templates" / "index.html"

    if not index_path.is_file():
        log(f"Error: index.html not found at {index_path}", "error")
        return web.Response(status=404, text="index.html not found")
    return web.FileResponse(index_path)


async def react_index_handler(_: web.Request) -> web.StreamResponse:
    """Serve the React frontend index.html for all React routes."""
    react_path = Path(__file__).parent / "frontend" / "index.html"
    if not react_path.is_file():
        return web.Response(status=404, text="React frontend not built yet. Run: npm run build")
    return web.FileResponse(react_path)


async def sse_text_handler(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events endpoint — streams {ar, en, hu} to React clients."""
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    queue: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_listeners.add(queue)
    log(f"SSE client connected (total: {len(_sse_listeners)})")
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=20)
                await response.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                # Send SSE comment keepalive every 20 s to prevent proxy/browser timeout
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, Exception):
        pass
    finally:
        _sse_listeners.discard(queue)
        log(f"SSE client disconnected (total: {len(_sse_listeners)})")
    return response


async def sse_audio_handler(request: web.Request) -> web.StreamResponse:
    """SSE endpoint that pushes TTS audio chunks to the TV display page."""
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    _audio_listeners.add(queue)
    log(f"Audio SSE client connected (total: {len(_audio_listeners)})")
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=25)
                await response.write(f"data: {payload}\n\n".encode())
            except asyncio.TimeoutError:
                await response.write(b": keepalive\n\n")
    except (ConnectionResetError, Exception):
        pass
    finally:
        _audio_listeners.discard(queue)
        log(f"Audio SSE client disconnected (total: {len(_audio_listeners)})")
    return response


async def tv_handler(_: web.Request) -> web.StreamResponse:
    """Serve the TV/display page from templates/tv.html."""
    tv_path = Path(__file__).parent / "templates" / "tv.html"
    if not tv_path.is_file():
        return web.Response(status=404, text="tv.html not found")
    return web.FileResponse(tv_path)


async def control_handler(request: web.Request) -> web.Response:
    """Phone-friendly REST API for remote start/stop/status."""
    action = request.match_info.get("action", "status")

    if action == "status":
        elapsed = None
        if _recording_state["started_at"]:
            elapsed = int(time.time() - _recording_state["started_at"])
        return web.json_response({
            "recording": _recording_state["active"],
            "elapsed_seconds": elapsed,
            "listeners": len(_sse_listeners),
        })

    if action == "start":
        log(f"[control] start: active={_recording_state['active']}, headless={_headless_client is not None}")
        if _recording_state["active"]:
            return web.json_response({"ok": False, "reason": "already_recording"})
        if _headless_client is None:
            return web.json_response({"ok": False, "reason": "server_not_ready"})
        http = request.app["http_client"]
        await start_recording(_headless_client, http)
        # Notify all open browser tabs that recording started
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(json.dumps({"type": "log", "level": "info", "message": "\u25b6 Recording started via phone control"}))
        return web.json_response({"ok": True, "action": "started"})

    if action == "stop":
        log(f"[control] stop: active={_recording_state['active']}, active_client={_active_recording_client is not None}")
        if not _recording_state["active"]:
            return web.json_response({"ok": False, "reason": "not_recording"})
        if _headless_client is None:
            return web.json_response({"ok": False, "reason": "server_not_ready"})
        await stop_recording(_headless_client)
        log("[control] stop: done")
        # Notify all open browser tabs that recording stopped
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(json.dumps({"type": "log", "level": "info", "message": "\u25a0 Recording stopped via phone control"}))
        return web.json_response({"ok": True, "action": "stopped"})

    return web.json_response({"ok": False, "reason": "unknown_action"}, status=400)


_XENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}

_BROWSER_PAGES = {
    "app":       "http://localhost/app",
    "tv":        "http://localhost/tv",
    "screen":    "http://localhost/screen",
    "screen-ar": "http://localhost/screen/ar",
    "screen-en": "http://localhost/screen/en",
    "screen-hu": "http://localhost/screen/hu",
    "operator":  "http://localhost/",
    "control":   "http://localhost/control",
}


async def _run(cmd: str) -> tuple[int, str]:
    """Run a shell command async, return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **_XENV},
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return proc.returncode, (out or b"").decode(errors="replace").strip()


async def browser_handler(request: web.Request) -> web.Response:
    """Control the Chromium kiosk browser from the phone.

    Actions:
      navigate/<page>  — open a page  (app | tv | operator | control)
      exit-kiosk       — reopen without --kiosk flag (windowed, escapable)
      kiosk            — reopen in kiosk/fullscreen mode
      refresh          — reload current page (Ctrl+R via xdotool)
      close            — kill the browser
    """
    action = request.match_info.get("action", "")

    browser_bin = "chromium-browser" if Path("/usr/bin/chromium-browser").exists() else "chromium"

    if action.startswith("navigate/"):
        page_key = action.split("/", 1)[1]
        url = _BROWSER_PAGES.get(page_key)
        if not url:
            return web.json_response({"ok": False, "reason": f"unknown page '{page_key}'"}, status=400)
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --kiosk --noerrdialogs --disable-infobars --no-first-run '{url}' &"
        )
        return web.json_response({"ok": True, "action": f"navigate:{url}"})

    if action == "exit-kiosk":
        # Reopen in a normal resizable window — user can then use Alt+F4 or close button
        url = _BROWSER_PAGES["app"]
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --start-maximized --noerrdialogs --no-first-run '{url}' &"
        )
        return web.json_response({"ok": True, "action": "exit-kiosk"})

    if action == "kiosk":
        url = _BROWSER_PAGES["app"]
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --kiosk --noerrdialogs --disable-infobars --no-first-run '{url}' &"
        )
        return web.json_response({"ok": True, "action": "kiosk"})

    if action == "refresh":
        rc, out = await _run(
            "xdotool search --onlyvisible --class chromium key --clearmodifiers ctrl+r"
        )
        return web.json_response({"ok": rc == 0, "action": "refresh", "detail": out})

    if action == "close":
        await _run("pkill -f chromium 2>/dev/null")
        return web.json_response({"ok": True, "action": "close"})

    if action.startswith("language/"):
        lang = action.split("/", 1)[1]  # "ar" | "en" | "hu" | "all"
        msg = json.dumps({"type": "lang_switch", "lang": lang})
        # Broadcast to all WS browser clients (index.html)
        for c in _all_ws_browsers:
            if not c.ws.closed:
                await c.ws.send_str(msg)
        # Also push to SSE listeners (tv.html)
        sse_payload = json.dumps({"lang_switch": lang})
        for q in list(_sse_listeners):
            try:
                q.put_nowait(sse_payload)
            except asyncio.QueueFull:
                pass
        return web.json_response({"ok": True, "lang": lang})

    return web.json_response({"ok": False, "reason": "unknown browser action"}, status=400)


async def server_control_handler(request: web.Request) -> web.Response:
    """Restart or get status of the juma systemd service."""
    action = request.match_info.get("action", "")

    if action == "restart":
        log("[server] restart requested via phone")
        # Schedule restart after response is sent so the response completes
        asyncio.get_event_loop().call_later(
            1.0, lambda: asyncio.ensure_future(_run("systemctl restart juma.service"))
        )
        return web.json_response({"ok": True, "action": "restart", "note": "restarting in 1s"})

    if action == "status":
        rc, out = await _run("systemctl is-active juma.service")
        return web.json_response({"ok": True, "active": rc == 0, "state": out})

    return web.json_response({"ok": False, "reason": "unknown server action"}, status=400)


async def control_page_handler(_: web.Request) -> web.StreamResponse:
    """Serve the phone control page (English)."""
    ctrl_path = Path(__file__).parent / "templates" / "control.html"
    if not ctrl_path.is_file():
        return web.Response(status=404, text="control.html not found")
    return web.FileResponse(ctrl_path)


async def control_page_ar_handler(_: web.Request) -> web.StreamResponse:
    """Serve the phone control page (Arabic)."""
    ctrl_path = Path(__file__).parent / "templates" / "control_ar.html"
    if not ctrl_path.is_file():
        return web.Response(status=404, text="control_ar.html not found")
    return web.FileResponse(ctrl_path)


async def screen_handler(_: web.Request) -> web.StreamResponse:
    """Serve the big-screen display page (Samsung TV / 50-inch monitor)."""
    screen_path = Path(__file__).parent / "templates" / "screen.html"
    if not screen_path.is_file():
        return web.Response(status=404, text="screen.html not found")
    return web.FileResponse(screen_path)


async def screen_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket for screen registration: assigns a 4-digit ID and relays set_lang commands."""
    import random

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Assign a unique 4-digit ID
    while True:
        sid = str(random.randint(1000, 9999))
        if sid not in _screen_clients:
            break

    page = request.query.get("page", "screen")
    sc = ScreenClient(screen_id=sid, ws=ws, page=page)
    _screen_clients[sid] = sc

    # Tell the screen its assigned ID
    await ws.send_str(json.dumps({"type": "init", "id": sid, "page": page}))
    log(f"[screen] {sid} connected (page={page})")

    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        _screen_clients.pop(sid, None)
        log(f"[screen] {sid} disconnected")

    return ws


async def get_screens_handler(_: web.Request) -> web.Response:
    """Return list of all currently connected display screens."""
    screens = [
        {"id": sc.screen_id, "page": sc.page, "lang": sc.lang}
        for sc in _screen_clients.values()
    ]
    return web.json_response(screens)


async def set_screen_lang_handler(request: web.Request) -> web.Response:
    """Set the language filter on a specific screen by ID."""
    screen_id = request.match_info["screen_id"]
    lang = request.match_info["lang"]  # "ar" | "en" | "hu" | "all"

    sc = _screen_clients.get(screen_id)
    if not sc:
        return web.json_response({"ok": False, "reason": f"Screen {screen_id} not connected"}, status=404)

    sc.lang = None if lang == "all" else lang
    try:
        await sc.ws.send_str(json.dumps({"type": "set_lang", "lang": lang}))
    except Exception as exc:
        return web.json_response({"ok": False, "reason": str(exc)}, status=500)

    return web.json_response({"ok": True, "id": screen_id, "lang": lang})


_SCREEN_CONFIGS: dict[str, dict] = {
    "ar": {
        "dir": "rtl",
        "label": "العربية · Arabic",
        "placeholder": "في انتظار التلاوة…",
        "font_class": "ar",
        "accent_color": "linear-gradient(90deg,#d4a843,#f0c060)",
    },
    "en": {
        "dir": "ltr",
        "label": "English",
        "placeholder": "Awaiting translation…",
        "font_class": "latin",
        "accent_color": "linear-gradient(90deg,#3b82f6,#60a5fa)",
    },
    "hu": {
        "dir": "ltr",
        "label": "Magyar",
        "placeholder": "Fordításra vár…",
        "font_class": "latin",
        "accent_color": "linear-gradient(90deg,#a855f7,#c084fc)",
    },
}


async def screen_single_handler(request: web.Request) -> web.Response:
    """Serve a single-language fullscreen display page (/screen/ar|en|hu)."""
    lang = request.match_info.get("lang", "")
    cfg = _SCREEN_CONFIGS.get(lang)
    if not cfg:
        return web.Response(status=404, text=f"Unknown language: {lang}")

    tpl_path = Path(__file__).parent / "templates" / "screen_single.html"
    if not tpl_path.is_file():
        return web.Response(status=404, text="screen_single.html not found")

    html = tpl_path.read_text(encoding="utf-8")
    for placeholder, value in [
        ("{{LANG}}", lang),
        ("{{DIR}}", cfg["dir"]),
        ("{{LABEL}}", cfg["label"]),
        ("{{PLACEHOLDER}}", cfg["placeholder"]),
        ("{{FONT_CLASS}}", cfg["font_class"]),
        ("{{ACCENT_COLOR}}", cfg["accent_color"]),
    ]:
        html = html.replace(placeholder, value)
    return web.Response(text=html, content_type="text/html", charset="utf-8")


class AudioStreamTrack(MediaStreamTrack):
    """A MediaStreamTrack that streams edge-tts TTS audio to a WebRTC peer."""

    kind = "audio"

    def __init__(self, tts_queue: asyncio.Queue):
        super().__init__()
        self._queue = tts_queue
        self._timestamp = 0
        self._sample_rate = 48000
        self._samples_per_frame = 960  # 20ms at 48kHz

    async def recv(self):
        import av as _av
        import numpy as _np

        # Wait for the next audio chunk (MP3 bytes) from TTS
        mp3_bytes: bytes = await self._queue.get()

        # Decode MP3 to PCM using PyAV
        container = _av.open(__import__("io").BytesIO(mp3_bytes))
        pcm_frames = []
        for frame in container.decode(audio=0):
            pcm_frames.append(frame.to_ndarray())
        container.close()

        if pcm_frames:
            pcm = _np.concatenate(pcm_frames, axis=1)
        else:
            pcm = _np.zeros((1, self._samples_per_frame), dtype=_np.float32)

        # Resample to 48kHz if needed (edge-tts outputs 24kHz)
        if pcm.shape[1] > 0:
            from fractions import Fraction as _Frac
            target_len = int(pcm.shape[1] * self._sample_rate / 24000)
            indices = _np.linspace(0, pcm.shape[1] - 1, target_len).astype(int)
            pcm = pcm[:, indices]

        # Build an AudioFrame
        frame = _av.AudioFrame.from_ndarray(pcm.astype(_np.float32), format="fltp", layout="mono")
        frame.sample_rate = self._sample_rate
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        self._timestamp += pcm.shape[1]
        return frame


async def webrtc_offer_handler(request: web.Request) -> web.Response:
    """Handle WebRTC offer from React frontend — stream TTS audio back."""
    params = await request.json()
    audio_lang = params.get("audioLang", "en")

    pc = RTCPeerConnection()
    _active_pcs.add(pc)

    # A queue that receives MP3 chunks from TTS synthesis
    tts_queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    track = AudioStreamTrack(tts_queue)
    pc.addTrack(track)

    # Store the queue on the app so _process_chunk can push to it
    lang_key = f"webrtc_tts_{audio_lang}"
    if lang_key not in request.app:
        request.app[lang_key] = set()
    request.app[lang_key].add(tts_queue)

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            _active_pcs.discard(pc)
            request.app.get(lang_key, set()).discard(tts_queue)
            await pc.close()

    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def on_startup(app: web.Application) -> None:
    """Create the shared HTTP client used for OpenRouter requests."""
    global _headless_client
    app["http_client"] = ClientSession()
    _headless_client = ClientState(ws=_NullWebSocket())  # type: ignore[arg-type]


async def on_cleanup(app: web.Application) -> None:
    """Close the shared HTTP client when the server shuts down."""
    http: ClientSession = app["http_client"]
    await http.close()
    # Close all open WebRTC peer connections
    for pc in list(_active_pcs):
        await pc.close()


def create_app() -> web.Application:
    """Build and wire the aiohttp application and its routes."""
    app = web.Application()

    # --- CORS (required for React frontend on any origin) ---
    cors_options = aiohttp_cors.ResourceOptions(
        allow_credentials=False,
        expose_headers="*",
        allow_headers="*",
        allow_methods=["GET", "POST", "OPTIONS"],
    )
    cors = aiohttp_cors.setup(app, defaults={
        "http://localhost:5175": cors_options,
        "http://localhost:5173": cors_options,

        "http://127.0.0.1:5175": cors_options,
        "http://127.0.0.1:5173": cors_options,

        "http://localhost:3000": cors_options,
        "http://127.0.0.1:3000": cors_options,
        "*": cors_options,
    })

    # Legacy aiohttp WebSocket UI (kept working)
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/stream", ws_handler)

    # SSE text stream
    sse_route = app.router.add_get("/stream/text", sse_text_handler)
    cors.add(sse_route)

    # SSE audio stream (TV page)
    audio_sse_route = app.router.add_get("/stream/audio", sse_audio_handler)
    cors.add(audio_sse_route)

    # TV display page
    app.router.add_get("/tv", tv_handler)

    # Big-screen display (50-inch Samsung TV)
    app.router.add_get("/screen", screen_handler)
    app.router.add_get("/screen/{lang}", screen_single_handler)

    # Screen WebSocket (for ID assignment + per-screen lang control)
    app.router.add_get("/ws/screen", screen_ws_handler)

    # Screen management API
    screens_route = app.router.add_get("/api/screens", get_screens_handler)
    cors.add(screens_route)
    screen_lang_route = app.router.add_get("/api/screen/{screen_id}/lang/{lang}", set_screen_lang_handler)
    cors.add(screen_lang_route)

    # Static fonts (for screen.html big-screen page)
    fonts_dir = Path(__file__).parent / "static" / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/fonts", fonts_dir, show_index=False)

    # Phone remote control page + API
    app.router.add_get("/control", control_page_handler)
    app.router.add_get("/control/ar", control_page_ar_handler)
    ctrl_route = app.router.add_get("/api/control/{action}", control_handler)
    cors.add(ctrl_route)
    browser_route = app.router.add_get("/api/browser/{action:.*}", browser_handler)
    cors.add(browser_route)
    srv_route = app.router.add_get("/api/server/{action}", server_control_handler)
    cors.add(srv_route)

    # WebRTC audio offer for React frontend
    webrtc_route = app.router.add_post("/webrtc/offer", webrtc_offer_handler)
    cors.add(webrtc_route)

    # React frontend static files (served at /app/)
    frontend_dir = Path(__file__).parent / "frontend"
    if frontend_dir.is_dir():
        app.router.add_get("/app", react_index_handler)
        app.router.add_get("/app/", react_index_handler)
        app.router.add_get("/app/tv", react_index_handler)
        app.router.add_static("/app/assets", frontend_dir / "assets", show_index=False)

    # Reports download
    app.router.add_static("/reports", Path(__file__).parent / "reports", show_index=True)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    log("Server is running on http://0.0.0.0:80")
    web.run_app(create_app(), host="0.0.0.0", port=80)