"""aiohttp routes, WebSocket control, and application factory."""

import asyncio
import json
import os
import time
from dataclasses import dataclass
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
        while not ws.closed:
            primary: Optional[ClientState] = request.app.get("last_ws_client")
            if primary is None or not primary.recording:
                await asyncio.sleep(0.2)
                continue
            if lang == "ar":
                return primary
            if primary.tts_queues is not None:
                return primary
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
    """Serve transcription and translation updates via Server-Sent Events (SSE)."""
    
    # 1. Set up the SSE headers with UTF-8 encoding
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

    # 2. Get the active recording session
    client = request.app.get("last_ws_client")
    if not client:
        log("No active recording session for SSE text stream")
        return response

    log("SSE text stream client connected")

    try:
        # Keep track of where we are in the chunk history
        last_chunk_count = len(client.chunk_history)

        while True:
            current_chunk_count = len(client.chunk_history)
            
            # If a new chunk was added by the audio_processor
            if current_chunk_count > last_chunk_count:
                latest_chunk = client.chunk_history[-1]
                data = {
                    "ar": latest_chunk.ar,
                    "en": latest_chunk.en,
                    "hu": latest_chunk.hu,
                }
                
                # SSE format requires "data: <json>\n\n" encoded to bytes
                # ensure_ascii=False prevents Arabic/Hungarian text from turning into \uXXXX codes
                sse_payload = f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")
                await response.write(sse_payload)
                
                # Update our tracker
                last_chunk_count = current_chunk_count

            # Sleep briefly to avoid blocking the event loop while polling
            await asyncio.sleep(0.2)

    except asyncio.CancelledError:
        log("SSE text stream client disconnected (browser closed/refreshed)")
    except ConnectionResetError:
        log("SSE text stream connection reset")
    finally:
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


# ── Display control broadcast (SSE → all frontend pages) ─────────────────────

@dataclass
class DisplaySubscriber:
    display_id: str
    queue: asyncio.Queue
    language: str = "all"
    page: str = "/tv"
    connected_at: float = 0.0


_displays: dict[str, DisplaySubscriber] = {}
_display_counter: int = 0


def _next_display_id() -> str:
    global _display_counter
    _display_counter += 1
    return str(_display_counter)


async def _broadcast_display_event(event: dict, target_id: str | None = None) -> int:
    """Push a control event to subscribers. If target_id set, only that display."""
    payload = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
    dead: list[str] = []
    sent = 0
    for did, sub in list(_displays.items()):
        if target_id and did != target_id:
            continue
        try:
            sub.queue.put_nowait(payload)
            sent += 1
        except asyncio.QueueFull:
            dead.append(did)
    for did in dead:
        _displays.pop(did, None)
    return sent


async def display_control_sse_handler(request: web.Request) -> web.StreamResponse:
    """SSE stream that frontend pages subscribe to for remote commands.

    Query params:
      ?id=<display_id>  — reuse existing ID (reconnect)
      ?page=<path>      — which page this display is showing
    """
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )
    await response.prepare(request)

    requested_id = request.query.get("id", "").strip()
    page = request.query.get("page", "/tv").strip()

    if requested_id and requested_id in _displays:
        display_id = requested_id
        sub = _displays[display_id]
        sub.queue = asyncio.Queue(maxsize=32)
        sub.page = page
    else:
        display_id = requested_id or _next_display_id()
        sub = DisplaySubscriber(
            display_id=display_id,
            queue=asyncio.Queue(maxsize=32),
            page=page,
            connected_at=time.monotonic(),
        )
        _displays[display_id] = sub

    log(f"Display '{display_id}' connected (page={page}, {len(_displays)} total)")

    # Send the display its assigned ID + current language setting
    welcome = f"data: {json.dumps({'command': 'welcome', 'displayId': display_id, 'language': sub.language})}\n\n"
    await response.write(welcome.encode("utf-8"))

    try:
        while True:
            payload = await sub.queue.get()
            await response.write(payload)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        _displays.pop(display_id, None)
        log(f"Display '{display_id}' disconnected ({len(_displays)} total)")

    return response


async def displays_list_handler(request: web.Request) -> web.Response:
    """List all connected displays and their current settings."""
    displays = [
        {
            "id": sub.display_id,
            "language": sub.language,
            "page": sub.page,
        }
        for sub in _displays.values()
    ]
    return web.json_response({"displays": displays, "count": len(displays)})


async def display_set_language_handler(request: web.Request) -> web.Response:
    """Set language for a specific display or all displays."""
    display_id = request.match_info.get("id", "")
    lang = request.query.get("lang", "all").strip()

    if lang not in ("ar", "en", "hu", "all"):
        return web.json_response({"ok": False, "reason": "invalid lang"}, status=400)

    if display_id == "all":
        for sub in _displays.values():
            sub.language = lang
        n = await _broadcast_display_event({"command": "language", "lang": lang})
        return web.json_response({"ok": True, "lang": lang, "targets": n})

    sub = _displays.get(display_id)
    if not sub:
        return web.json_response({"ok": False, "reason": "display not found"}, status=404)

    sub.language = lang
    await _broadcast_display_event({"command": "language", "lang": lang}, target_id=display_id)
    return web.json_response({"ok": True, "displayId": display_id, "lang": lang})


# ── Pi-specific shell helpers (optional, used alongside SSE broadcast) ────────

_XENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}

def _browser_pages() -> dict[str, str]:
    """URLs the Pi's local browser navigates to. Uses nginx (port 80) if available, else app port."""
    port = os.getenv("RTT_ALHUDA_LISTEN_PORT", "3000").strip()
    base = "http://localhost" if port == "80" else f"http://localhost:{port}"
    return {
        "app":      f"{base}/app",
        "tv":       f"{base}/tv",
        "operator": f"{base}/",
        "control":  f"{base}/control",
    }


def _is_pi() -> bool:
    return Path("/usr/bin/chromium-browser").exists() or Path("/usr/bin/chromium").exists()


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
            "displays": len(_displays),
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
    """Control all frontend displays from the phone.

    Broadcasts commands via SSE to all subscribed pages.
    Also runs Pi-specific shell commands when on a Raspberry Pi.

    Actions:
      navigate/<page>  — tell all displays to navigate to a page
      refresh          — tell all displays to reload
      close            — tell all displays to close/blank
      language/<lang>  — switch display language (ar|en|hu|all)
      exit-kiosk       — (Pi only) reopen browser without --kiosk
      kiosk            — (Pi only) reopen browser in kiosk mode
    """
    action = request.match_info.get("action", "")

    if action.startswith("navigate/"):
        page_key = action.split("/", 1)[1]
        page_paths = {"app": "/app", "tv": "/tv", "operator": "/", "control": "/control"}
        path = page_paths.get(page_key)
        if not path:
            return web.json_response(
                {"ok": False, "reason": f"unknown page '{page_key}'"}, status=400
            )
        n = await _broadcast_display_event({"command": "navigate", "path": path, "page": page_key})
        if _is_pi():
            pages = _browser_pages()
            url = pages[page_key]
            browser_bin = "chromium-browser" if Path("/usr/bin/chromium-browser").exists() else "chromium"
            await _run("pkill -f chromium 2>/dev/null; sleep 1")
            await _run(
                f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
                f"{browser_bin} --kiosk --noerrdialogs --disable-infobars "
                f"--no-first-run '{url}' &"
            )
        return web.json_response({"ok": True, "action": f"navigate:{page_key}", "subscribers": n})

    if action == "refresh":
        n = await _broadcast_display_event({"command": "refresh"})
        if _is_pi():
            await _run(
                "xdotool search --onlyvisible --class chromium "
                "key --clearmodifiers ctrl+r"
            )
        return web.json_response({"ok": True, "action": "refresh", "subscribers": n})

    if action == "close":
        n = await _broadcast_display_event({"command": "close"})
        if _is_pi():
            await _run("pkill -f chromium 2>/dev/null")
        return web.json_response({"ok": True, "action": "close", "subscribers": n})

    if action.startswith("language/"):
        lang = action.split("/", 1)[1]
        for sub in _displays.values():
            sub.language = lang
        n = await _broadcast_display_event({"command": "language", "lang": lang})
        return web.json_response({"ok": True, "lang": lang, "subscribers": n})

    if action == "exit-kiosk":
        if _is_pi():
            browser_bin = "chromium-browser" if Path("/usr/bin/chromium-browser").exists() else "chromium"
            url = _browser_pages()["app"]
            await _run("pkill -f chromium 2>/dev/null; sleep 1")
            await _run(
                f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
                f"{browser_bin} --start-maximized --noerrdialogs --no-first-run '{url}' &"
            )
        return web.json_response({"ok": True, "action": "exit-kiosk"})

    if action == "kiosk":
        if _is_pi():
            browser_bin = "chromium-browser" if Path("/usr/bin/chromium-browser").exists() else "chromium"
            url = _browser_pages()["app"]
            await _run("pkill -f chromium 2>/dev/null; sleep 1")
            await _run(
                f"sudo -u pi DISPLAY=:0 XAUTHORITY=/home/pi/.Xauthority "
                f"{browser_bin} --kiosk --noerrdialogs --disable-infobars "
                f"--no-first-run '{url}' &"
            )
        return web.json_response({"ok": True, "action": "kiosk"})

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
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/api/lan-ipv4", lan_ipv4_handler)
    app.router.add_get("/stream", ws_handler)
    app.router.add_get(r"/stream/tts/{lang}", tts_stream_handler)
    app.router.add_get("/stream/text", text_stream_handler)
    app.router.add_get("/stream/display-control", display_control_sse_handler)
    # Control dashboard (English + Arabic)
    app.router.add_get("/control", control_page_handler)
    app.router.add_get("/control_ar", control_ar_page_handler)
    # Control REST API
    app.router.add_get("/api/control/{action}", control_handler)
    app.router.add_get("/api/displays", displays_list_handler)
    app.router.add_get("/api/display/{id}/language", display_set_language_handler)
    app.router.add_get("/api/browser/{action:.*}", browser_handler)
    app.router.add_get("/api/server/{action}", server_control_handler)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
