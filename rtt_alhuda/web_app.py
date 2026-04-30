"""aiohttp routes, WebSocket control, and application factory."""

import asyncio
import json
import time
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

from rtt_alhuda.audio_capture import capture_microphone_loop
from rtt_alhuda.audio_processor import process_audio_loop
from rtt_alhuda.audio_stream_ws import mic_ws_sender, tts_ws_sender
from rtt_alhuda.config import REPO_ROOT
from rtt_alhuda.models import ClientState
from rtt_alhuda.web_protocol import send_log
from rtt_alhuda.openrouter_debug import log_startup_summary


def get_hours_timestamp() -> str:
    """Return a human-readable timestamp used in server logs."""

    return time.strftime("%H:%M:%S")


def log(*parts: object) -> None:
    """Print a timestamped log line to stdout."""

    print(f"[{get_hours_timestamp()}]", *parts)


async def stop_recording(client: ClientState) -> None:
    """Stop the active recording and cancel background tasks for the client."""

    client.recording = False

    tasks = [
        task
        for task in (
            client.recorder_task,
            client.processor_task,
            client.mic_sender_task,
            client.tts_sender_task,
        )
        if task
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    client.recorder_task = None
    client.processor_task = None
    client.mic_sender_task = None
    client.tts_sender_task = None
    client.media_mic_queue = None
    client.media_tts_queue = None
    client.ws_mic_subscribed = False
    client.ws_tts_subscribed = False


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
    client.ws_mic_subscribed = False
    client.ws_tts_subscribed = False

    client.media_mic_queue = asyncio.Queue(maxsize=50)
    client.media_tts_queue = asyncio.Queue(maxsize=8)

    client.recorder_task = asyncio.create_task(capture_microphone_loop(client))
    client.processor_task = asyncio.create_task(process_audio_loop(client, http))
    client.mic_sender_task = asyncio.create_task(mic_ws_sender(client))
    client.tts_sender_task = asyncio.create_task(tts_ws_sender(client))
    await send_log(client, "Recording started")


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle browser control messages for a single WebSocket session."""

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    http: ClientSession = request.app["http_client"]
    client = ClientState(ws=ws)
    request.app["last_ws_client"] = client

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
                    lang_raw = payload.get("ttsLanguage") or payload.get("tts_language") or "en"
                    if isinstance(lang_raw, str) and lang_raw.lower() in ("hu", "hungarian"):
                        client.media_tts_language = "hu"
                    else:
                        client.media_tts_language = "en"
                    await start_recording(client, http)
                elif msg_type == "stop":
                    await stop_recording(client)
                    await send_log(client, "Recording stopped")
                elif msg_type == "subscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        client.ws_mic_subscribed = True
                    elif stream == "tts":
                        client.ws_tts_subscribed = True
                    else:
                        await send_log(client, f"Unknown stream: {stream}", "warn")
                elif msg_type == "unsubscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        client.ws_mic_subscribed = False
                    elif stream == "tts":
                        client.ws_tts_subscribed = False
                    else:
                        await send_log(client, f"Unknown stream: {stream}", "warn")
                else:
                    await send_log(client, f"Unknown message type: {msg_type}", "warn")
            elif msg.type == WSMsgType.ERROR:
                await send_log(client, f"WebSocket error: {ws.exception()}", "error")
    finally:
        await stop_recording(client)
        if request.app.get("last_ws_client") is client:
            request.app["last_ws_client"] = None
        log("WebSocket client disconnected")

    return ws


def _template_response(name: str) -> web.StreamResponse:
    path = REPO_ROOT / "templates" / name
    if not path.is_file():
        log(f"Error: template not found at {path}", "error")
        return web.Response(status=404, text=f"{name} not found")
    return web.FileResponse(path)


async def index_handler(_: web.Request) -> web.StreamResponse:
    """Serve the browser UI from templates/index.html."""

    return _template_response("index.html")



async def on_startup(app: web.Application) -> None:
    """Create the shared HTTP client used for OpenRouter requests."""

    app["http_client"] = ClientSession()
    log_startup_summary()


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
