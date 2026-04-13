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
import time
import traceback
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Optional

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from dotenv import load_dotenv

import webrtcvad

vad = webrtcvad.Vad(2)  # aggressiveness 0-3 (2 is aggressive)

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
PROCESSING_INTERVAL_SECONDS = 2
AUDIO_WINDOW_SECONDS = 6
CHUNK_OVERLAP_SECONDS = 1.0
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
FRAME_CHUNK_SECONDS = 0.1
MAX_BUFFER_SECONDS = AUDIO_WINDOW_SECONDS + CHUNK_OVERLAP_SECONDS + 6


def is_speech_present(pcm_data: bytes) -> bool:
    """Check if a PCM audio chunk contains speech using WebRTC VAD."""
    try:
        # WebRTC VAD requires exact frame durations: 10, 20, or 30 ms.
        frame_duration_ms = 30
        frame_bytes = int((SAMPLE_RATE * frame_duration_ms / 1000.0) * CHANNELS * SAMPLE_WIDTH_BYTES)
        
        speech_frames = 0
        total_frames = 0
        
        for i in range(0, len(pcm_data) - frame_bytes + 1, frame_bytes):
            frame = pcm_data[i:i+frame_bytes]
            if vad.is_speech(frame, SAMPLE_RATE):
                speech_frames += 1
            total_frames += 1
            
        # If less than ~5% of frames contain speech, we consider it silent noise.
        speech_ratio = speech_frames / total_frames if total_frames > 0 else 0
        return speech_ratio >= 0.05
    except Exception as e:
        print(f"VAD error: {e}")
        return True  # Fallback to true so we don't drop audio falsely


@dataclass
class ClientState:
    """Per-WebSocket runtime state for one connected browser session."""

    ws: web.WebSocketResponse
    pcm_buffer: bytearray = field(default_factory=bytearray)
    buffer_start_sample: int = 0
    total_samples_written: int = 0
    transcription_buffer: list[str] = field(default_factory=list)
    translation_buffer: list[str] = field(default_factory=list)
    recorder_task: Optional[asyncio.Task] = None
    processor_task: Optional[asyncio.Task] = None
    recording: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_chunk_end_sample: Optional[int] = None


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
) -> dict:
    """Send one audio window to OpenRouter and return the parsed JSON result."""

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY environment variable")

    system = """
    You are a live transcriber and translator.
     
    You will be given an audio stream chunk and an existing transcription and translation. Transcribe and translate the new words
    in the audio with Arabic transcription (أ ب ت ...), and English translation.
     
    YOUR JOB: append only the NEW words that have been said in the given audio.

    SUPER IMPORTANT: Do NOT add words that have NOT been said in the audio! And Do NOT repeat text!!. 
    ONLY respond with the new additional transcription and translation that is IN THE AUDIO and NOT included in the original transcription and translation. 
    If the the audio contains speech that is already written in the original transcription, DO NOT repeat it again in your response.

    SUPER IMPORTANT: You must be careful if the audio is silent, or unclear, or noisy (contains no speech) or just background noise, then you MUST return empty strings (exactly {"new_additional_transcription": "", "new_additional_translation": ""}) and nothing else! even if original sentence is not completed! (mind you that most of the times it will be empty audio!)

    Do NOT autocomplete! or hallucinate your own words or interpret unclear words! Only append what is actually spoken in the audio! 
    
    Ignore the last second (two or three words) of the audio, to avoid incomplete sentences (they will be included in the next chunk with more context).
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
    processed_sample_count: int,
    original_transcription: str,
    original_translation: str,
    chunk_end_sample: Optional[int],
):
    chunk_duration_seconds = processed_sample_count / SAMPLE_RATE
    try:
        start_time = time.time()
        result = await send_chunk_to_openrouter(
            http,
            wav_b64,
            original_transcription,
            original_translation,
        )
        latency_ms = int((time.time() - start_time) * 1000)

        new_transcription = str(result.get("new_additional_transcription", ""))
        new_translation = str(result.get("new_additional_translation", ""))

        client.transcription_buffer.append(new_transcription)
        client.translation_buffer.append(new_translation)

        message = {
            "type": "transcription",
            "transcription": new_transcription,
            "translation": new_translation,
            "originalTranscription": original_transcription,
            "originalTranslation": original_translation,
            "rawResponse": json.dumps(result),
            "originalAudioChunk": wav_b64,
            "processedChunks": 1,
            "windowSeconds": AUDIO_WINDOW_SECONDS,
            "chunkDurationSeconds": chunk_duration_seconds,
            "latencyMs": latency_ms,
        }
        if not client.ws.closed:
            await client.ws.send_str(json.dumps(message))

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
        f"Audio processing started (every {PROCESSING_INTERVAL_SECONDS}s, window {AUDIO_WINDOW_SECONDS}s)",
    )

    try:
        while client.recording and not client.ws.closed:
            await asyncio.sleep(PROCESSING_INTERVAL_SECONDS)

            window_samples = int(SAMPLE_RATE * AUDIO_WINDOW_SECONDS)
            overlap_samples = int(SAMPLE_RATE * CHUNK_OVERLAP_SECONDS)
            bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

            async with client.lock:
                end_sample = client.total_samples_written
                available_samples = end_sample - client.buffer_start_sample
                if available_samples <= 0:
                    chunk_pcm = b""
                    processed_sample_count = 0
                    chunk_end_sample = None
                else:
                    start_sample = max(
                        client.buffer_start_sample,
                        end_sample - window_samples,
                    )

                    start_offset_samples = start_sample - client.buffer_start_sample
                    end_offset_samples = end_sample - client.buffer_start_sample
                    start_byte = start_offset_samples * bytes_per_frame
                    end_byte = end_offset_samples * bytes_per_frame
                    chunk_pcm = bytes(client.pcm_buffer[start_byte:end_byte])
                    processed_sample_count = max(0, end_sample - start_sample)
                    chunk_end_sample = end_sample

            if not chunk_pcm:
                continue

            # --- WebRTC VAD check ---
            if not is_speech_present(chunk_pcm):
                await send_log(client, "Silent audio chunk detected by VAD, skipping LLM request.")
                async with client.lock:
                    if chunk_end_sample is not None:
                        client.last_chunk_end_sample = chunk_end_sample
                continue
            # ------------------------

            wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
            wav_b64 = base64.b64encode(wav_bytes).decode("ascii")

            original_transcription = " ".join(client.transcription_buffer[-2:])
            original_translation = " ".join(client.translation_buffer[-2:])

            await _process_chunk(
                client,
                http,
                wav_b64,
                processed_sample_count,
                original_transcription,
                original_translation,
                chunk_end_sample,
            )

            async with client.lock:
                if chunk_end_sample is not None:
                    client.last_chunk_end_sample = chunk_end_sample

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
    client.transcription_buffer.clear()
    client.translation_buffer.clear()
    client.last_chunk_end_sample = None

    client.recorder_task = asyncio.create_task(capture_microphone_loop(client))
    client.processor_task = asyncio.create_task(process_audio_loop(client, http))
    await send_log(client, "Recording started")


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


async def on_startup(app: web.Application) -> None:
    """Create the shared HTTP client used for OpenRouter requests."""

    app["http_client"] = ClientSession()


async def on_cleanup(app: web.Application) -> None:
    """Close the shared HTTP client when the server shuts down."""

    http: ClientSession = app["http_client"]
    await http.close()


def create_app() -> web.Application:
    """Build and wire the aiohttp application and its routes."""

    app = web.Application()
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/stream", ws_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    log("Server is running on http://localhost:3000")
    web.run_app(create_app(), host="127.0.0.1", port=3000)