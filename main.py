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
from typing import Optional, Set

# Work around a Windows + Python 3.14 issue where platform.system()
# can block inside a WMI query and make aiohttp import appear stuck.
if os.name == "nt" and sys.version_info >= (3, 14):
    platform.system = lambda: "Windows"

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web
from dotenv import load_dotenv

import webrtcvad

vad = webrtcvad.Vad(2)  # aggressiveness 0-3 (2 is aggressive)

load_dotenv(dotenv_path=Path(__file__).with_name(".env"), override=False)


OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
PROCESSING_INTERVAL_SECONDS = 3
CONTEXT_CHUNK_COUNT = 2  # Number of past chunks to include in the payload for context
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
FRAME_CHUNK_SECONDS = 0.1
MAX_BUFFER_SECONDS = 120  # Increased to prevent dropping audio during API delays


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
class ChunkInfo:
    """Stores exact sample boundaries and results for a transcribed audio chunk."""
    start_sample: int
    end_sample: int
    transcription: str
    translation: str


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

    SUPER IMPORTANT: Do NOT add words that have NOT been said in the audio! And Do NOT repeat what is already in the original transcription/translation!. 
    ONLY respond with the new additional transcription and translation that is IN THE AUDIO and NOT included in the original transcription and translation. 
    If the the audio contains speech that is already written in the original transcription, DO NOT repeat it again in your response.

    SUPER IMPORTANT: You must be careful if the audio is silent, or unclear, or noisy (contains no speech) or just background noise, then you MUST return empty strings (exactly {"new_additional_transcription": "", "new_additional_translation": ""}) and nothing else! even if original sentence is not completed! (mind you that most of the times it will be empty audio!)

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
    new_audio_start_sample: int,
    new_audio_end_sample: int,
    original_transcription: str,
    original_translation: str,
    chunk_duration_seconds: float,
):
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

        async with client.lock:
            client.chunk_history.append(
                ChunkInfo(
                    start_sample=new_audio_start_sample,
                    end_sample=new_audio_end_sample,
                    transcription=new_transcription,
                    translation=new_translation,
                )
            )

        message = {
            "type": "transcription",
            "transcription": new_transcription,
            "translation": new_translation,
            "originalTranscription": original_transcription,
            "originalTranslation": original_translation,
            "rawResponse": json.dumps(result),
            "originalAudioChunk": wav_b64,
            "processedChunks": 1,
            "windowSeconds": chunk_duration_seconds,
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
                else:
                    past_start_sample = client.last_chunk_end_sample
                    original_transcription = ""
                    original_translation = ""

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


# ==============================================================================
# NEW WEBRTC ADDITIONS
# ==============================================================================

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame
import aiohttp_cors

class LiveAudioStreamTrack(MediaStreamTrack):
    """A dummy WebRTC audio track that streams silent frames to keep the connection alive."""
    kind = "audio"
    
    def __init__(self):
        super().__init__()
        self._timestamp = 0

    async def recv(self):
        samples = int(48000 * 0.02)
        frame = AudioFrame(format='s16', layout='mono', samples=samples)
        for plane in frame.planes:
            plane.update(bytes(samples * 2))
        frame.pts = self._timestamp
        frame.time_base = 1 / 48000
        self._timestamp += samples
        return frame

class WebRTCManager:
    """Manages WebRTC Peer Connections for the frontend."""
    def __init__(self):
        self.pcs = set()

    async def handle_offer(self, request: web.Request) -> web.Response:
        params = await request.json()
        log(f"WebRTC Handshake received. Audio Lang: {params.get('audioLang', 'auto')}")
        
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
        pc = RTCPeerConnection()
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            if pc.connectionState in ["failed", "closed"]:
                self.pcs.discard(pc)

        pc.addTrack(LiveAudioStreamTrack())
        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return web.json_response({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type
        })

    async def cleanup(self):
        coros = [pc.close() for pc in self.pcs]
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        self.pcs.clear()

webrtc_manager = WebRTCManager()

@dataclass
class WebRTCChunkInfo:
    """Stores the 3-language translations for the WebRTC pipeline."""
    start_sample: int
    end_sample: int
    ar: str
    en: str
    hu: str

@dataclass
class GlobalState:
    """Global state for the WebRTC/SSE pipeline endpoints."""
    is_recording: bool = False
    pcm_buffer: bytearray = field(default_factory=bytearray)
    buffer_start_sample: int = 0
    total_samples_written: int = 0
    last_chunk_end_sample: int = 0
    chunk_history: list[WebRTCChunkInfo] = field(default_factory=list)
    text_listeners: Set[asyncio.Queue] = field(default_factory=set)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mic_task: Optional[asyncio.Task] = None
    proc_task: Optional[asyncio.Task] = None

webrtc_state = GlobalState()

async def capture_mic_webrtc_loop() -> None:
    """Captures microphone audio locally into the global WebRTC state."""
    import sounddevice as sd
    frame_chunk = int(SAMPLE_RATE * FRAME_CHUNK_SECONDS)
    max_buffer_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16", blocksize=frame_chunk) as stream:
            log("WebRTC: Microphone Active.")
            while webrtc_state.is_recording:
                try:
                    data, overflowed = await asyncio.to_thread(stream.read, frame_chunk)
                except Exception:
                    break
                
                pcm_bytes = bytes(data)
                sample_count = len(pcm_bytes) // bytes_per_frame
                
                async with webrtc_state.lock:
                    webrtc_state.pcm_buffer.extend(pcm_bytes)
                    webrtc_state.total_samples_written += sample_count
                    available = webrtc_state.total_samples_written - webrtc_state.buffer_start_sample
                    if available > max_buffer_samples:
                        overflow = available - max_buffer_samples
                        del webrtc_state.pcm_buffer[:overflow * bytes_per_frame]
                        webrtc_state.buffer_start_sample += overflow
    except Exception as e:
        if "Unanticipated host error" not in str(e):
            log(f"WebRTC Mic Error: {e}")
    finally:
        webrtc_state.is_recording = False


async def send_chunk_to_openrouter_webrtc(http: ClientSession, audio_b64_wav: str, orig_ar: str, orig_en: str, orig_hu: str) -> dict:
    """Detects which of the 3 languages is spoken, transcribes it, and translates to the other 2."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return {"ar": "", "en": "", "hu": ""}

    system = """
    ACT AS A MULTILINGUAL AUDIO-TO-TEXT CONVERTER AND TRANSLATOR.
     
    The spoken audio will be in Arabic, English, or Hungarian.

    YOUR JOB:
    1. Detect the spoken language in the audio.
    2. Transcribe ONLY the NEW words in the audio into that language's matching JSON key ('ar', 'en', or 'hu').
    3. Translate those new words into the OTHER TWO languages.

    RULES:
    - DO NOT repeat words from the Context. Only output NEW words.
    - If the audio is silent or unclear, return empty strings for all fields: {"ar": "", "en": "", "hu": ""}.
    - Do not autocomplete or hallucinate.
    """
    
    body = {
        "model": OPENROUTER_MODEL,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": f'"Context AR": "{orig_ar}", "Context EN": "{orig_en}", "Context HU": "{orig_hu}"'},
                {"type": "input_audio", "input_audio": {"data": audio_b64_wav, "format": "wav"}},
            ]},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "live_transcription_result",
                "strict": True,
                "schema": {
                    "type": "object",
                    "required": ["ar", "en", "hu"],
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

    try:
        async with http.post(OPENROUTER_API_URL, json=body, headers={"Authorization": f"Bearer {api_key}"}) as resp:
            if resp.status == 200:
                res_json = await resp.json()
                content = res_json['choices'][0]['message']['content']
                return json.loads(content)
    except Exception as e:
        log(f"WebRTC API Error: {e}")
    return {"ar": "", "en": "", "hu": ""}


async def process_audio_webrtc_loop(http: ClientSession) -> None:
    """Processes buffered audio and broadcasts the 3 languages via SSE."""
    log("WebRTC: AI Processing Started")
    next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS

    while webrtc_state.is_recording:
        wait_seconds = next_cycle_at - time.monotonic()
        if wait_seconds > 0: await asyncio.sleep(wait_seconds)

        bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES
        async with webrtc_state.lock:
            end_sample = webrtc_state.total_samples_written
            chunk_pcm = b""
            new_audio_pcm = b""
            
            history = webrtc_state.chunk_history[-CONTEXT_CHUNK_COUNT:] if CONTEXT_CHUNK_COUNT > 0 else []
            past_start = history[0].start_sample if history else webrtc_state.last_chunk_end_sample
            
            # Extract all 3 languages for context
            orig_ar = " ".join(c.ar for c in history if c.ar).strip()
            orig_en = " ".join(c.en for c in history if c.en).strip()
            orig_hu = " ".join(c.hu for c in history if c.hu).strip()

            new_start = webrtc_state.last_chunk_end_sample
            
            if (end_sample - new_start) >= int(SAMPLE_RATE * PROCESSING_INTERVAL_SECONDS):
                start_sample = max(webrtc_state.buffer_start_sample, past_start)
                chunk_pcm = bytes(webrtc_state.pcm_buffer[(start_sample - webrtc_state.buffer_start_sample) * bytes_per_frame : (end_sample - webrtc_state.buffer_start_sample) * bytes_per_frame])
                safe_new_start = max(webrtc_state.buffer_start_sample, new_start)
                new_audio_pcm = bytes(webrtc_state.pcm_buffer[(safe_new_start - webrtc_state.buffer_start_sample) * bytes_per_frame : (end_sample - webrtc_state.buffer_start_sample) * bytes_per_frame])

        if not chunk_pcm:
            next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
            continue

        if not is_speech_present(new_audio_pcm):
            log("WebRTC: Silent audio chunk, skipping request.")
            async with webrtc_state.lock: webrtc_state.last_chunk_end_sample = end_sample
            next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
            continue

        wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
        request_started = time.monotonic()

        # Pass all 3 context histories to OpenRouter
        result = await send_chunk_to_openrouter_webrtc(http, base64.b64encode(wav_bytes).decode("ascii"), orig_ar, orig_en, orig_hu)

        log(f"WebRTC AI Response: {result}")

        new_ar = str(result.get("ar", "")).strip()
        new_en = str(result.get("en", "")).strip()
        new_hu = str(result.get("hu", "")).strip()
        
        if new_ar or new_en or new_hu:
            async with webrtc_state.lock:
                webrtc_state.chunk_history.append(WebRTCChunkInfo(start_sample=new_start, end_sample=end_sample, ar=new_ar, en=new_en, hu=new_hu))
            
            for q in list(webrtc_state.text_listeners):
                await q.put(result)

        async with webrtc_state.lock: webrtc_state.last_chunk_end_sample = end_sample
        next_cycle_at = request_started + PROCESSING_INTERVAL_SECONDS


async def start_handler(request: web.Request) -> web.Response:
    if not webrtc_state.is_recording:
        webrtc_state.is_recording = True
        webrtc_state.pcm_buffer.clear()
        webrtc_state.buffer_start_sample = 0
        webrtc_state.total_samples_written = 0
        webrtc_state.last_chunk_end_sample = 0
        webrtc_state.chunk_history.clear()
        
        http: ClientSession = request.app["http_client"]
        webrtc_state.mic_task = asyncio.create_task(capture_mic_webrtc_loop())
        webrtc_state.proc_task = asyncio.create_task(process_audio_webrtc_loop(http))
        log("WebRTC Session Started via POST /start")
    return web.json_response({"ok": True, "recording": True})

async def stop_handler(request: web.Request) -> web.Response:
    webrtc_state.is_recording = False
    
    if webrtc_state.proc_task and not webrtc_state.proc_task.done():
        webrtc_state.proc_task.cancel()
        
    webrtc_state.proc_task = None
    webrtc_state.mic_task = None
    
    log("WebRTC Session Stopped via POST /stop")
    return web.json_response({"ok": True, "recording": False})

async def sse_handler(request: web.Request) -> web.StreamResponse:
    # Removed the manually hardcoded Access-Control-Allow-Origin header 
    # to avoid conflicting with aiohttp_cors library.
    response = web.StreamResponse(headers={
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
    })
    await response.prepare(request)
    
    log("WebRTC: SSE Text Stream Connected!")
    
    queue = asyncio.Queue()
    webrtc_state.text_listeners.add(queue)
    
    try:
        while True:
            data = await queue.get()
            formatted = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            await response.write(formatted.encode('utf-8'))
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if "Cannot write to closing transport" not in str(e) and "ConnectionReset" not in str(type(e).__name__):
            log(f"SSE Interrupted: {e}")
    finally:
        webrtc_state.text_listeners.remove(queue)
        
    return response


# ==============================================================================
# APP WIRING & DUAL SERVER STARTUP
# ==============================================================================

async def index_handler(_: web.Request) -> web.StreamResponse:
    """Serve the original WebSocket UI from templates/index.html."""
    index_path = Path(__file__).parent / "templates" / "index.html"
    if not index_path.is_file():
        log(f"Error: index.html not found at {index_path}", "error")
        return web.Response(status=404, text="index.html not found")
    return web.FileResponse(index_path)

async def webrtc_page_handler(_: web.Request) -> web.StreamResponse:
    """Serve the new WebRTC UI from templates/test_webRTC.html."""
    webrtc_path = Path(__file__).parent / "templates" / "test_webRTC.html"
    if not webrtc_path.is_file():
        log(f"Error: test_webRTC.html not found at {webrtc_path}", "error")
        return web.Response(status=404, text="test_webRTC.html not found")
    return web.FileResponse(webrtc_path)

async def on_startup(app: web.Application) -> None:
    app["http_client"] = ClientSession()

async def on_cleanup_ws(app: web.Application) -> None:
    await app["http_client"].close()

async def on_cleanup_webrtc(app: web.Application) -> None:
    await app["http_client"].close()
    await webrtc_manager.cleanup()

async def start_dual_servers():
    """Starts two independent applications on port 3000 and 5021 simultaneously."""
    
    # ---------------------------------------------------------
    # APP 1: Original WebSocket Server (Port 3000)
    # ---------------------------------------------------------
    app_ws = web.Application()
    app_ws.router.add_get("/", index_handler)               # Root loads index.html
    app_ws.router.add_get("/index.html", index_handler)
    app_ws.router.add_get("/stream", ws_handler)
    app_ws.on_startup.append(on_startup)
    app_ws.on_cleanup.append(on_cleanup_ws)
    
    runner_ws = web.AppRunner(app_ws)
    await runner_ws.setup()
    site_3000 = web.TCPSite(runner_ws, "127.0.0.1", 3000)
    await site_3000.start()
    log("Original WebSocket Server is running on http://127.0.0.1:3000")

    # ---------------------------------------------------------
    # APP 2: New WebRTC Server (Port 5021)
    # ---------------------------------------------------------
    app_webrtc = web.Application()
    cors = aiohttp_cors.setup(app_webrtc, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
    })
    
    app_webrtc.router.add_get("/", webrtc_page_handler)     # Root loads test_webRTC.html
    app_webrtc.router.add_get("/test_webRTC.html", webrtc_page_handler)
    
    # Let cors manage the SSE handler
    cors.add(app_webrtc.router.add_get("/stream/text", sse_handler))
    cors.add(app_webrtc.router.add_post("/webrtc/offer", webrtc_manager.handle_offer))
    cors.add(app_webrtc.router.add_post("/start", start_handler))
    cors.add(app_webrtc.router.add_post("/stop", stop_handler))
    
    app_webrtc.on_startup.append(on_startup)
    app_webrtc.on_cleanup.append(on_cleanup_webrtc)
    
    runner_webrtc = web.AppRunner(app_webrtc)
    await runner_webrtc.setup()
    site_5021 = web.TCPSite(runner_webrtc, "127.0.0.1", 5021)
    await site_5021.start()
    log("New WebRTC Server is running on http://127.0.0.1:5021")

    # Keep the event loop running
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    try:
        asyncio.run(start_dual_servers())
    except KeyboardInterrupt:
        log("Servers shutting down...")
