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

import edge_tts
from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
import aiohttp_cors
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
# TTS Configuration
# =========================================================================
EDGE_TTS_EN_VOICE = "en-US-AndrewMultilingualNeural"  # Natural US male voice
EDGE_TTS_HU_VOICE = "hu-HU-TamasNeural"  # Hungarian male neural voice


async def synthesize_edge_tts(text: str, voice: str) -> bytes | None:
    """Generate MP3 audio using Microsoft Edge neural TTS.
    
    Splits long text into ~500 char segments at sentence boundaries
    to avoid crashes with very long text.
    """
    if not text or not text.strip():
        return None
    try:
        # Split long text into segments at sentence boundaries
        segments = _split_text_for_tts(text, max_chars=500)
        all_audio = []
        for seg in segments:
            communicate = edge_tts.Communicate(seg, voice)
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    all_audio.append(chunk["data"])
        if all_audio:
            return b"".join(all_audio)
        return None
    except Exception as e:
        log(f"Edge TTS ({voice}) error: {e}")
        return None


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


# Global set of SSE queues — one per connected React frontend client
_sse_listeners: set[asyncio.Queue] = set()
_active_pcs: set = set()  # Track open RTCPeerConnections


async def _broadcast_translations(ar: str, en: str, hu: str) -> None:
    """Push latest translations to all SSE listeners."""
    payload = json.dumps({"ar": ar, "en": en, "hu": hu})
    dead = set()
    for q in _sse_listeners:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _sse_listeners.difference_update(dead)


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

    print(f"[{get_hours_timestamp()}]", *parts)


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
            while client.recording and not client.ws.closed:
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
    You are a live transcriber and translator.
     
    You will be given an audio stream chunk and an existing transcription and translations. Transcribe and translate the new words
    in the audio with Arabic transcription (أ ب ت ...), English translation, AND Hungarian translation.
     
    YOUR JOB: append only the NEW words that have been said in the given audio.

    SUPER IMPORTANT: Do NOT add words that have NOT been said in the audio! And Do NOT repeat what is already in the original transcription/translations!. 
    ONLY respond with the new additional transcription and translations that are IN THE AUDIO and NOT included in the original transcription and translations. 
    If the the audio contains speech that is already written in the original transcription, DO NOT repeat it again in your response.

    SUPER IMPORTANT: You must be careful if the audio is silent, or unclear, or noisy (contains no speech) or just background noise, then you MUST return empty strings (exactly {"new_additional_transcription": "", "new_additional_translation": "", "new_additional_translation_hu": ""}) and nothing else! even if original sentence is not completed! (mind you that most of the times it will be empty audio!)

    Do NOT autocomplete! or hallucinate your own words or interpret unclear words! Only append what is actually spoken in the audio! 
    
    DO NOT include the last second (incomplete words/sentences) of the audio (they will be sent again in the next chunk with more context).
    And don't add new lines.
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
        "HTTP-Referer": "http://localhost:3000",
        "X-Title": "rtt-alhuda-node",
    }

    async with http.post(
        OPENROUTER_API_URL,
        json=body,
        headers=headers,
        timeout=ClientTimeout(total=120),
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

        # Generate TTS for both languages in parallel
        tts_en_bytes, tts_hu_bytes = await asyncio.gather(
            synthesize_edge_tts(new_translation, EDGE_TTS_EN_VOICE) if new_translation.strip() else asyncio.sleep(0, result=None),
            synthesize_edge_tts(new_translation_hu, EDGE_TTS_HU_VOICE) if new_translation_hu.strip() else asyncio.sleep(0, result=None),
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
            await _broadcast_translations(full_ar, full_en, full_hu)

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
        # Add TTS audio as base64 (for legacy WebSocket UI)
        if tts_en_bytes:
            message["ttsEnAudio"] = base64.b64encode(tts_en_bytes).decode("ascii")
        if tts_hu_bytes:
            message["ttsHuAudio"] = base64.b64encode(tts_hu_bytes).decode("ascii")

        if not client.ws.closed:
            await client.ws.send_str(json.dumps(message))

        # Push TTS audio to any connected WebRTC audio streams (React frontend)
        if tts_en_bytes:
            for q in list(client.ws._req.app.get("webrtc_tts_en", set())):
                try:
                    q.put_nowait(tts_en_bytes)
                except asyncio.QueueFull:
                    pass
        if tts_hu_bytes:
            for q in list(client.ws._req.app.get("webrtc_tts_hu", set())):
                try:
                    q.put_nowait(tts_hu_bytes)
                except asyncio.QueueFull:
                    pass

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

        while client.recording and not client.ws.closed:
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

    client.recording = False

    tasks = [task for task in (client.recorder_task, client.processor_task) if task]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    client.recorder_task = None
    client.processor_task = None


async def start_recording(client: ClientState, http: ClientSession) -> None:
    """Reset client state and start microphone capture plus audio processing."""

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


async def generate_final_report(client: ClientState) -> None:
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

    # Generate full TTS audio files (may take time for long text)
    try:
        await send_log(client, "Generating audio files (this may take a minute)...")
        for lang_text, voice, suffix in [
            (full_english, EDGE_TTS_EN_VOICE, "english"),
            (full_hungarian, EDGE_TTS_HU_VOICE, "hungarian"),
        ]:
            if not lang_text:
                continue
            audio_data = await synthesize_edge_tts(lang_text, voice)
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

    await send_log(client, f"Report complete! {len(files_created)} files generated.")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle browser control messages for a single WebSocket session."""

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    http: ClientSession = request.app["http_client"]
    client = ClientState(ws=ws)

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
                    await generate_final_report(client)
                else:
                    await send_log(client, f"Unknown message type: {msg_type}", "warn")
            elif msg.type == WSMsgType.ERROR:
                await send_log(client, f"WebSocket error: {ws.exception()}", "error")
    finally:
        await stop_recording(client)
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
            payload = await asyncio.wait_for(queue.get(), timeout=25)
            await response.write(f"data: {payload}\n\n".encode())
    except asyncio.TimeoutError:
        # Send a keepalive comment to prevent browser timeout
        try:
            await response.write(b": keepalive\n\n")
        except Exception:
            pass
        # Re-enter the loop — handled by finally if client disconnected
        try:
            while True:
                payload = await asyncio.wait_for(queue.get(), timeout=25)
                await response.write(f"data: {payload}\n\n".encode())
        except (asyncio.TimeoutError, ConnectionResetError, Exception):
            pass
    except (ConnectionResetError, Exception):
        pass
    finally:
        _sse_listeners.discard(queue)
        log(f"SSE client disconnected (total: {len(_sse_listeners)})")
    return response


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
    app["http_client"] = ClientSession()


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
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["GET", "POST", "OPTIONS"],
        )
    })

    # Legacy aiohttp WebSocket UI (kept working)
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/stream", ws_handler)

    # SSE text stream for React frontend
    sse_route = app.router.add_get("/stream/text", sse_text_handler)
    cors.add(sse_route)

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