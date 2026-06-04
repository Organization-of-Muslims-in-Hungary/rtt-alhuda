"""aiohttp routes, WebSocket control, and application factory."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import ClientSession, WSMsgType, web

from rtt_alhuda.audio_capture import capture_microphone_loop
from rtt_alhuda.audio_processor import process_audio_loop
from rtt_alhuda.audio_stream_ws import (
    mic_original_fanout_loop,
    mic_ws_sender,
    tts_fanout_loop,
    tts_ws_sender,
)
from rtt_alhuda.config import REPO_ROOT
from rtt_alhuda.lan_detect import detect_lan_ipv4
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

    if client.tts_fanout_tasks:
        for t in list(client.tts_fanout_tasks.values()):
            if t and not t.done():
                t.cancel()
        await asyncio.gather(
            *client.tts_fanout_tasks.values(),
            return_exceptions=True,
        )
        client.tts_fanout_tasks = None
    client.tts_queues = None

    if client.original_fanout_task and not client.original_fanout_task.done():
        client.original_fanout_task.cancel()
        await asyncio.gather(
            client.original_fanout_task,
            return_exceptions=True,
        )
    client.original_fanout_task = None
    client.original_pcm_queue = None

    async with client.lock:
        for _lang, socks in list(client.tts_satellites.items()):
            for sat_ws in list(socks):
                if not sat_ws.closed:
                    await sat_ws.close(code=1001)
            socks.clear()
        for sat_ws in list(client.original_audio_satellites):
            if not sat_ws.closed:
                await sat_ws.close(code=1001)
        client.original_audio_satellites.clear()

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
    client.original_pcm_queue = asyncio.Queue(maxsize=50)
    client.original_fanout_task = asyncio.create_task(
        mic_original_fanout_loop(client),
        name="mic-original-fanout",
    )
    client.tts_queues = {
        "en": asyncio.Queue(maxsize=8),
        "hu": asyncio.Queue(maxsize=8),
    }
    client.tts_fanout_tasks = {
        lang: asyncio.create_task(
            tts_fanout_loop(client, lang),
            name=f"tts-fanout-{lang}",
        )
        for lang in ("en", "hu")
    }

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
    client.text_sse_clients = request.app["text_sse_clients"]
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


async def tts_stream_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket: ``en``/``hu`` → MP3 (0x02). ``ar`` → live mic PCM (0x01), not TTS."""

    lang = (request.match_info.get("lang") or "").lower()
    if lang not in ("ar", "en", "hu"):
        raise web.HTTPBadRequest(text="lang must be ar, en, or hu")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    async def wait_active_primary() -> Optional[ClientState]:
        ping_interval = 0
        while not ws.closed:
            primary: Optional[ClientState] = request.app.get("last_ws_client")
            if primary is not None and primary.recording:
                if lang == "ar" or primary.tts_queues is not None:
                    return primary

            # Keep the WebSocket alive while waiting for a session.
            ping_interval += 1
            if ping_interval >= 50:          # ~10 s at 0.2 s sleeps
                await ws.ping()
                ping_interval = 0

            await asyncio.sleep(0.2)
        return None

    primary = await wait_active_primary()
    if primary is None or ws.closed:
        if not ws.closed:
            await ws.close()
        return ws

    async with primary.lock:
        if lang == "ar":
            primary.original_audio_satellites.add(ws)
        else:
            primary.tts_satellites[lang].add(ws)

    try:
        async for msg in ws:
            if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    finally:
        async with primary.lock:
            if lang == "ar":
                primary.original_audio_satellites.discard(ws)
            else:
                primary.tts_satellites[lang].discard(ws)
        if not ws.closed:
            await ws.close()
    return ws


async def text_stream_handler(request: web.Request) -> web.StreamResponse:
    """Serve transcription and translation updates via Server-Sent Events (SSE).

    Data is pushed directly from ``_process_chunk`` — no polling.
    This handler just registers the response and keeps the connection alive.
    """

    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )
    await response.prepare(request)

    sse_set: set = request.app["text_sse_clients"]
    sse_set.add(response)
    log("SSE text stream client connected")

    try:
        # Keep the connection alive with periodic SSE comments.
        # aiohttp cancels this coroutine when the client disconnects.
        while True:
            await asyncio.sleep(15)
            await response.write(b": keepalive\n\n")
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError):
        pass
    finally:
        sse_set.discard(response)
        log("SSE text stream client disconnected")

    return response

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


async def control_page_handler(_: web.Request) -> web.StreamResponse:
    """Serve the English phone control page."""
    return _template_response("control.html")


async def control_ar_page_handler(_: web.Request) -> web.StreamResponse:
    """Serve the Arabic phone control page."""
    return _template_response("control_ar.html")


# ── Pi-specific control helpers ───────────────────────────────────────────────

_XENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}

_BROWSER_PAGES = {
    "app":      "http://localhost/app",
    "tv":       "http://localhost/tv",
    "operator": "http://localhost/",
    "control":  "http://localhost/control",
}


async def _run(cmd: str) -> tuple[int, str]:
    """Run a shell command asynchronously; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **_XENV},
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return proc.returncode, (out or b"").decode(errors="replace").strip()


async def control_handler(request: web.Request) -> web.Response:
    """Phone-friendly REST API for remote start/stop/status of recording."""
    action = request.match_info.get("action", "status")
    client: ClientState | None = request.app.get("last_ws_client")

    if action == "status":
        recording = bool(client and client.recording)
        return web.json_response({
            "recording": recording,
            "connected": client is not None,
        })

    if action == "start":
        if client is None:
            return web.json_response({"ok": False, "reason": "no_client_connected"})
        if client.recording:
            return web.json_response({"ok": False, "reason": "already_recording"})
        http: ClientSession = request.app["http_client"]
        await start_recording(client, http)
        return web.json_response({"ok": True, "action": "started"})

    if action == "stop":
        if client is None:
            return web.json_response({"ok": False, "reason": "no_client_connected"})
        if not client.recording:
            return web.json_response({"ok": False, "reason": "not_recording"})
        await stop_recording(client)
        return web.json_response({"ok": True, "action": "stopped"})

    return web.json_response({"ok": False, "reason": "unknown_action"}, status=400)


async def browser_handler(request: web.Request) -> web.Response:
    """Control the Chromium kiosk browser from the phone.

    Actions:
      navigate/<page>  — open a named page  (app | tv | operator | control)
      exit-kiosk       — reopen without --kiosk flag (windowed, escapable)
      kiosk            — reopen in kiosk/fullscreen mode
      refresh          — reload current page via xdotool Ctrl+R
      close            — kill the browser
      language/<lang>  — broadcast lang_switch to the active WebSocket client
    """
    action = request.match_info.get("action", "")
    browser_bin = (
        "chromium-browser"
        if Path("/usr/bin/chromium-browser").exists()
        else "chromium"
    )

    if action.startswith("navigate/"):
        page_key = action.split("/", 1)[1]
        url = _BROWSER_PAGES.get(page_key)
        if not url:
            return web.json_response(
                {"ok": False, "reason": f"unknown page '{page_key}'"}, status=400
            )
        await _run("pkill -f chromium 2>/dev/null; sleep 1")
        await _run(
            f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
            f"{browser_bin} --kiosk --noerrdialogs --disable-infobars "
            f"--no-first-run '{url}' &"
        )
        return web.json_response({"ok": True, "action": f"navigate:{url}"})

    if action == "exit-kiosk":
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
            f"{browser_bin} --kiosk --noerrdialogs --disable-infobars "
            f"--no-first-run '{url}' &"
        )
        return web.json_response({"ok": True, "action": "kiosk"})

    if action == "refresh":
        rc, out = await _run(
            "xdotool search --onlyvisible --class chromium "
            "key --clearmodifiers ctrl+r"
        )
        return web.json_response({"ok": rc == 0, "action": "refresh", "detail": out})

    if action == "close":
        await _run("pkill -f chromium 2>/dev/null")
        return web.json_response({"ok": True, "action": "close"})

    if action.startswith("language/"):
        lang = action.split("/", 1)[1]
        client: ClientState | None = request.app.get("last_ws_client")
        if client and not client.ws.closed:
            await client.ws.send_str(json.dumps({"type": "lang_switch", "lang": lang}))
        return web.json_response({"ok": True, "lang": lang})

    return web.json_response(
        {"ok": False, "reason": "unknown browser action"}, status=400
    )


async def server_control_handler(request: web.Request) -> web.Response:
    """Restart or get the status of the juma systemd service."""
    action = request.match_info.get("action", "")

    if action == "restart":
        log("[server] restart requested via phone control")
        asyncio.get_event_loop().call_later(
            1.0,
            lambda: asyncio.ensure_future(_run("systemctl restart juma.service")),
        )
        return web.json_response(
            {"ok": True, "action": "restart", "note": "restarting in 1s"}
        )

    if action == "status":
        rc, out = await _run("systemctl is-active juma.service")
        return web.json_response({"ok": True, "active": rc == 0, "state": out})

    return web.json_response(
        {"ok": False, "reason": "unknown server action"}, status=400
    )


async def lan_ipv4_handler(_request: web.Request) -> web.Response:
    """JSON for dev QR codes: ``{"ipv4": "<addr>" | null}`` — same-LAN as this server."""

    return web.json_response({"ipv4": detect_lan_ipv4()})


def create_app() -> web.Application:
    """Build and wire the aiohttp application and its routes."""

    app = web.Application()
    app["text_sse_clients"] = set()
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/api/lan-ipv4", lan_ipv4_handler)
    app.router.add_get("/stream", ws_handler)
    app.router.add_get(r"/stream/tts/{lang}", tts_stream_handler)
    app.router.add_get("/stream/text", text_stream_handler) # Add this line
    # Control dashboard (English + Arabic)
    app.router.add_get("/control", control_page_handler)
    app.router.add_get("/control_ar", control_ar_page_handler)
    # Control REST API
    app.router.add_get("/api/control/{action}", control_handler)
    app.router.add_get("/api/browser/{action:.*}", browser_handler)
    app.router.add_get("/api/server/{action}", server_control_handler)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
