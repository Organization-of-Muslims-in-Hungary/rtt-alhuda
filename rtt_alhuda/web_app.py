"""aiohttp routes, WebSocket control, and application factory."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

from aiohttp import ClientSession, WSMsgType, web
import aiosqlite

from rtt_alhuda.audio_capture import capture_microphone_loop, feed_remote_audio
from rtt_alhuda.audio_processor import process_audio_loop
from rtt_alhuda.audio_stream_ws import (
    mic_original_fanout_loop,
    mic_ws_sender,
    tts_fanout_loop,
    tts_ws_sender,
)
from rtt_alhuda.config import REPO_ROOT
from rtt_alhuda.lan_detect import detect_lan_ipv4
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import send_log, send_sse_control
from rtt_alhuda.openrouter_debug import log_startup_summary
from rtt_alhuda import db as client_db
from rtt_alhuda.auth import (
    auth_middleware,
    clear_auth_cookie,
    create_access_token,
    hash_password,
    public_user,
    resolve_user,
    set_auth_cookie,
    validate_password,
    validate_username,
    verify_password,
)
from rtt_alhuda import config
from rtt_alhuda.config import validate_auth_config


def get_hours_timestamp() -> str:
    """Return a human-readable timestamp used in server logs."""

    return time.strftime("%H:%M:%S")


def log(*parts: object) -> None:
    """Print a timestamped log line to stdout."""

    print(f"[{get_hours_timestamp()}]", *parts)


def _get_session(request: web.Request) -> ServerSession:
    """Return the single server-owned session from the application."""
    return request.app["session"]


async def stop_recording(session: ServerSession) -> None:
    """Stop recording and cancel all background tasks on the session."""

    session.recording = False

    if session.tts_fanout_tasks:
        for t in list(session.tts_fanout_tasks.values()):
            if t and not t.done():
                t.cancel()
        await asyncio.gather(
            *session.tts_fanout_tasks.values(),
            return_exceptions=True,
        )
        session.tts_fanout_tasks = None
    session.tts_queues = None

    if session.original_fanout_task and not session.original_fanout_task.done():
        session.original_fanout_task.cancel()
        await asyncio.gather(
            session.original_fanout_task,
            return_exceptions=True,
        )
    session.original_fanout_task = None
    session.original_pcm_queue = None

    async with session.lock:
        for _lang, socks in list(session.tts_satellites.items()):
            for sat_ws in list(socks):
                if not sat_ws.closed:
                    await sat_ws.close(code=1001)
            socks.clear()
        for sat_ws in list(session.original_audio_satellites):
            if not sat_ws.closed:
                await sat_ws.close(code=1001)
        session.original_audio_satellites.clear()

    tasks = [
        task
        for task in (
            session.recorder_task,
            session.processor_task,
            session.mic_sender_task,
            session.tts_sender_task,
        )
        if task
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    session.recorder_task = None
    session.processor_task = None
    session.mic_sender_task = None
    session.tts_sender_task = None
    session.media_mic_queue = None
    session.media_tts_queue = None


async def start_recording(
    session: ServerSession,
    http: ClientSession,
    audio_source: str = "internal",
) -> None:
    """Reset session state and start microphone capture plus audio processing.

    *audio_source* controls where PCM comes from:
      - ``"internal"`` — the server's local microphone (sounddevice)
      - ``"remote"``  — PCM pushed over a WebSocket from the frontend.
    """

    if session.recording:
        await send_log(session, "Recording already running", "warn")
        return

    session.recording = True
    session.audio_source = audio_source
    session.pcm_buffer.clear()
    session.buffer_start_sample = 0
    session.total_samples_written = 0
    session.chunk_history.clear()
    session.last_chunk_end_sample = 0

    session.media_mic_queue = asyncio.Queue(maxsize=50)
    session.media_tts_queue = asyncio.Queue(maxsize=8)
    session.original_pcm_queue = asyncio.Queue(maxsize=50)
    session.original_fanout_task = asyncio.create_task(
        mic_original_fanout_loop(session),
        name="mic-original-fanout",
    )
    session.tts_queues = {
        "en": asyncio.Queue(maxsize=8),
        "hu": asyncio.Queue(maxsize=8),
    }
    session.tts_fanout_tasks = {
        lang: asyncio.create_task(
            tts_fanout_loop(session, lang),
            name=f"tts-fanout-{lang}",
        )
        for lang in ("en", "hu")
    }

    if audio_source == "internal":
        session.recorder_task = asyncio.create_task(capture_microphone_loop(session))
    else:
        session.recorder_task = None

    session.processor_task = asyncio.create_task(process_audio_loop(session, http))
    session.mic_sender_task = asyncio.create_task(mic_ws_sender(session))
    session.tts_sender_task = asyncio.create_task(tts_ws_sender(session))

    await send_log(
        session,
        f"Recording started (audio_source={audio_source})",
    )


async def debug_ws_handler(request: web.Request) -> web.WebSocketResponse:
    """Debug WebSocket: observe logs/transcriptions, optionally subscribe to audio.

    Multiple clients can connect simultaneously.  Disconnecting does NOT
    affect recording — the session is server-owned.
    """

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session = _get_session(request)
    session.debug_ws_clients.add(ws)

    await send_log(session, "Debug WebSocket connected")
    log("Debug WebSocket client connected")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await send_log(session, "Invalid JSON message", "warn")
                    continue

                msg_type = payload.get("type")
                if msg_type == "start":
                    lang_raw = (
                        payload.get("ttsLanguage")
                        or payload.get("tts_language")
                        or "en"
                    )
                    if isinstance(lang_raw, str) and lang_raw.lower() in (
                        "hu",
                        "hungarian",
                    ):
                        session.media_tts_language = "hu"
                    else:
                        session.media_tts_language = "en"
                    audio_source = payload.get("audio_source", "internal")
                    if audio_source not in ("internal", "remote"):
                        audio_source = "internal"
                    http: ClientSession = request.app["http_client"]
                    await start_recording(session, http, audio_source=audio_source)
                elif msg_type == "stop":
                    await stop_recording(session)
                    await send_log(session, "Recording stopped")
                elif msg_type == "subscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        session.mic_subscribers.add(ws)
                    elif stream == "tts":
                        session.tts_subscribers.add(ws)
                    else:
                        await send_log(
                            session, f"Unknown stream: {stream}", "warn"
                        )
                elif msg_type == "unsubscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        session.mic_subscribers.discard(ws)
                    elif stream == "tts":
                        session.tts_subscribers.discard(ws)
                    else:
                        await send_log(
                            session, f"Unknown stream: {stream}", "warn"
                        )
                else:
                    await send_log(
                        session, f"Unknown message type: {msg_type}", "warn"
                    )
            elif msg.type == WSMsgType.BINARY:
                data = msg.data
                if isinstance(data, (bytes, bytearray)) and len(data) > 1:
                    prefix_byte = data[0]
                    if prefix_byte == 0x03 and session.recording:
                        await feed_remote_audio(session, bytes(data[1:]))
            elif msg.type == WSMsgType.ERROR:
                await send_log(
                    session, f"WebSocket error: {ws.exception()}", "error"
                )
    finally:
        session.debug_ws_clients.discard(ws)
        session.mic_subscribers.discard(ws)
        session.tts_subscribers.discard(ws)
        log("Debug WebSocket client disconnected")

    return ws


async def tts_stream_handler(request: web.Request) -> web.WebSocketResponse:
    """WebSocket: ``en``/``hu`` -> MP3 (0x02). ``ar`` -> live mic PCM (0x01), not TTS."""

    lang = (request.match_info.get("lang") or "").lower()
    if lang not in ("ar", "en", "hu"):
        raise web.HTTPBadRequest(text="lang must be ar, en, or hu")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    session = _get_session(request)

    async def wait_recording() -> bool:
        """Wait until the session is recording (or the satellite disconnects)."""
        ping_interval = 0
        while not ws.closed:
            if session.recording:
                if lang == "ar" or session.tts_queues is not None:
                    return True
            ping_interval += 1
            if ping_interval >= 50:          # ~10 s at 0.2 s sleeps
                await ws.ping()
                ping_interval = 0
            await asyncio.sleep(0.2)
        return False

    if not await wait_recording():
        if not ws.closed:
            await ws.close()
        return ws

    async with session.lock:
        if lang == "ar":
            session.original_audio_satellites.add(ws)
        else:
            session.tts_satellites[lang].add(ws)

    try:
        while not ws.closed:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=20)
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR,
                                WSMsgType.CLOSING):
                    break
            except asyncio.TimeoutError:
                if ws.closed:
                    break
                try:
                    await ws.ping()
                except Exception:
                    break
    finally:
        async with session.lock:
            if lang == "ar":
                session.original_audio_satellites.discard(ws)
            else:
                session.tts_satellites[lang].discard(ws)
        if not ws.closed:
            await ws.close()
    return ws


async def text_stream_handler(request: web.Request) -> web.StreamResponse:
    """Serve transcription and translation updates via Server-Sent Events (SSE).

    Data is pushed directly from ``_process_chunk`` — no polling.
    This handler just registers the response and keeps the connection alive.
    SSE clients are fully independent of any WebSocket connection.

    Auto-registration:
        Every SSE connection is automatically registered as a client.
        Pass ``?client_id=<id>`` to re-identify (from localStorage).
        Pass ``?screen_w=N&screen_h=N`` for device-type detection.
        Pass ``?name=MyTV`` to give the client a friendly name.

        The server emits an ``event: registered`` SSE message with the
        client record (including ``id``) so the frontend can persist it.
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

    session = _get_session(request)
    session.text_sse_clients.add(response)

    # ── Auto-register the client ──────────────────────────────────
    db = request.app["client_db"]
    req_client_id = request.query.get("client_id") or None
    name = request.query.get("name", "")
    try:
        screen_w = int(request.query.get("screen_w", 0))
    except ValueError:
        screen_w = 0
    try:
        screen_h = int(request.query.get("screen_h", 0))
    except ValueError:
        screen_h = 0
    ua = request.headers.get("User-Agent", "")

    client = await client_db.register_client(
        db,
        client_id=req_client_id,
        name=name,
        screen_w=screen_w,
        screen_h=screen_h,
        user_agent=ua,
    )
    client_id = client["id"]
    session.client_sse_map.setdefault(client_id, set()).add(response)
    log(f"SSE text stream client connected (client_id={client_id})")

    # Send the client its registration info so it can store the id.
    reg_msg = f"event: registered\ndata: {json.dumps(client, ensure_ascii=False)}\n\n".encode()
    try:
        await response.write(reg_msg)
    except Exception:
        pass

    try:
        while True:
            await asyncio.sleep(5)
            await response.write(b": keepalive\n\n")
            await client_db.touch_client(db, client_id)
    except (asyncio.CancelledError, ConnectionResetError, ConnectionError,
            OSError, RuntimeError):
        pass
    finally:
        session.text_sse_clients.discard(response)
        sse_set = session.client_sse_map.get(client_id)
        if sse_set is not None:
            sse_set.discard(response)
            if not sse_set:
                del session.client_sse_map[client_id]
        log(f"SSE text stream client disconnected (client_id={client_id})")

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
    """Create the shared HTTP client and open the client database."""

    validate_auth_config()
    app["http_client"] = ClientSession()
    app["client_db"] = await client_db.get_db()
    seeded = await client_db.seed_default_admin(
        app["client_db"],
        config.DEFAULT_ADMIN_USERNAME,
        hash_password(config.DEFAULT_ADMIN_PASSWORD),
    )
    if seeded:
        log(f"Seeded default admin user '{config.DEFAULT_ADMIN_USERNAME}'")
    log_startup_summary()
    log(f"Client database: {client_db.DB_PATH}")


async def on_cleanup(app: web.Application) -> None:
    """Close the shared HTTP client and database when the server shuts down."""

    http: ClientSession = app["http_client"]
    await http.close()
    db = app.get("client_db")
    if db:
        await db.close()


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
    """Phone-friendly REST API for remote start/stop/status of recording.

    Operates directly on the server session — no WebSocket needed.
    """
    action = request.match_info.get("action", "status")
    session = _get_session(request)

    if action == "status":
        return web.json_response({
            "recording": session.recording,
        })

    if action == "start":
        if session.recording:
            return web.json_response({"ok": False, "reason": "already_recording"})
        source = request.query.get("source", "internal")
        if source not in ("internal", "remote"):
            return web.json_response(
                {"ok": False, "reason": "invalid_source"}, status=400
            )
        http: ClientSession = request.app["http_client"]
        await start_recording(session, http, audio_source=source)
        return web.json_response({"ok": True, "action": "started"})

    if action == "stop":
        if not session.recording:
            return web.json_response({"ok": False, "reason": "not_recording"})
        await stop_recording(session)
        return web.json_response({"ok": True, "action": "stopped"})

    return web.json_response({"ok": False, "reason": "unknown_action"}, status=400)


async def browser_handler(request: web.Request) -> web.Response:
    """Control the display from the phone.

    SSE-based (OS-independent — works on any TV/browser):
      navigate/<page>  — tell SSE clients to switch view  (app | tv | operator | control)
      refresh          — tell SSE clients to reload the page
      language/<lang>  — set TTS language + tell SSE clients to switch display language

    OS-level (Pi-specific, requires Linux + X11):
      exit-kiosk       — reopen Chromium without --kiosk flag
      kiosk            — reopen Chromium in kiosk/fullscreen mode
      close            — kill the browser process
    """
    action = request.match_info.get("action", "")
    session = _get_session(request)

    # ── SSE-based actions (OS-independent) ────────────────────────
    # Optional ?target=<client_id> to send to a single client instead of all.
    target = request.query.get("target")

    if action.startswith("navigate/"):
        page_key = action.split("/", 1)[1]
        if page_key not in _BROWSER_PAGES:
            return web.json_response(
                {"ok": False, "reason": f"unknown page '{page_key}'"}, status=400
            )
        await send_sse_control(session, "navigate", target_client_id=target, page=page_key)
        return web.json_response({"ok": True, "action": f"navigate:{page_key}", "target": target})

    if action == "refresh":
        await send_sse_control(session, "refresh", target_client_id=target)
        return web.json_response({"ok": True, "action": "refresh", "target": target})

    if action.startswith("language/"):
        lang = action.split("/", 1)[1]
        if not target:
            session.media_tts_language = lang
        await send_sse_control(session, "lang_switch", target_client_id=target, lang=lang)
        return web.json_response({"ok": True, "lang": lang, "target": target})

    # ── OS-level actions (Pi-specific) ────────────────────────────

    browser_bin = (
        "chromium-browser"
        if Path("/usr/bin/chromium-browser").exists()
        else "chromium"
    )

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

    if action == "close":
        await _run("pkill -f chromium 2>/dev/null")
        return web.json_response({"ok": True, "action": "close"})

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


async def network_status_handler(request: web.Request) -> web.Response:
    """Return live connection counts for SSE, debug WS, and satellite clients."""

    session = _get_session(request)

    async with session.lock:
        ws_satellites = {
            "ar": sum(
                1 for w in session.original_audio_satellites if not w.closed
            ),
            "en": sum(
                1 for w in session.tts_satellites.get("en", ()) if not w.closed
            ),
            "hu": sum(
                1 for w in session.tts_satellites.get("hu", ()) if not w.closed
            ),
        }

    debug_count = sum(1 for w in session.debug_ws_clients if not w.closed)

    return web.json_response({
        "sse_clients": len(session.text_sse_clients),
        "debug_ws_clients": debug_count,
        "ws_recording": session.recording,
        "ws_satellites": ws_satellites,
    })


async def lan_ipv4_handler(_request: web.Request) -> web.Response:
    """JSON for dev QR codes: ``{"ipv4": "<addr>" | null}`` — same-LAN as this server."""

    return web.json_response({"ipv4": detect_lan_ipv4()})


async def health_handler(_request: web.Request) -> web.Response:
    """GET /api/health — liveness probe for compose/systemd."""

    return web.json_response({"ok": True, "status": "healthy"})


# ── Client registry API ───────────────────────────────────────────────────────


async def client_register_handler(request: web.Request) -> web.Response:
    """POST /api/clients/register  — register or re-identify a client.

    Body JSON: {client_id?: string, name: string, screen_w: int, screen_h: int}
    Returns the full client record (with id to persist in localStorage).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "reason": "invalid json"}, status=400)

    db = request.app["client_db"]
    ua = request.headers.get("User-Agent", "")
    client = await client_db.register_client(
        db,
        client_id=body.get("client_id"),
        name=body.get("name", ""),
        screen_w=body.get("screen_w", 0),
        screen_h=body.get("screen_h", 0),
        user_agent=ua,
    )
    return web.json_response({"ok": True, "client": client})


async def client_list_handler(request: web.Request) -> web.Response:
    """GET /api/clients — list all known clients with online status."""
    db = request.app["client_db"]
    session = _get_session(request)
    clients = await client_db.list_clients(db)
    connected_ids = set(session.client_sse_map.keys())
    for c in clients:
        c["connected"] = c["id"] in connected_ids
    return web.json_response({"ok": True, "clients": clients})


async def client_rename_handler(request: web.Request) -> web.Response:
    """POST /api/clients/{client_id}/rename  — rename a client."""
    cid = request.match_info["client_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "reason": "invalid json"}, status=400)
    db = request.app["client_db"]
    ok = await client_db.rename_client(db, cid, body.get("name", ""))
    if not ok:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    return web.json_response({"ok": True})


async def client_delete_handler(request: web.Request) -> web.Response:
    """DELETE /api/clients/{client_id} — remove a client from registry."""
    cid = request.match_info["client_id"]
    db = request.app["client_db"]
    ok = await client_db.delete_client(db, cid)
    if not ok:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    return web.json_response({"ok": True})


# ── Operator authentication ─────────────────────────────────────────────────────


async def auth_register_handler(request: web.Request) -> web.Response:
    """POST /api/auth/register — open operator sign-up (pending admin approval)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "reason": "invalid json"}, status=400)

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    if err := validate_username(username):
        return web.json_response({"ok": False, "reason": err}, status=400)
    if err := validate_password(password):
        return web.json_response({"ok": False, "reason": err}, status=400)

    db = request.app["client_db"]
    if await client_db.get_user_by_username(db, username):
        return web.json_response({"ok": False, "reason": "username_taken"}, status=409)

    try:
        user = await client_db.create_user(
            db,
            username,
            hash_password(password),
            role="operator",
            status="pending",
        )
    except aiosqlite.IntegrityError:
        return web.json_response({"ok": False, "reason": "username_taken"}, status=409)

    return web.json_response(
        {"ok": True, "user": public_user(user)},
        status=201,
    )


async def auth_login_handler(request: web.Request) -> web.Response:
    """POST /api/auth/login — approved operators and admins receive JWT + cookie."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "reason": "invalid json"}, status=400)

    username = (body.get("username") or "").strip()
    password = body.get("password") or ""

    db = request.app["client_db"]
    user = await client_db.get_user_by_username(db, username)
    if not user or not verify_password(password, user["password_hash"]):
        return web.json_response({"ok": False, "reason": "invalid_credentials"}, status=401)

    if user["status"] == "pending":
        return web.json_response({"ok": False, "reason": "pending_approval"}, status=403)
    if user["status"] == "rejected":
        return web.json_response({"ok": False, "reason": "rejected"}, status=403)

    token = create_access_token(user)
    response = web.json_response({"ok": True, "token": token, "user": public_user(user)})
    set_auth_cookie(response, token)
    return response


async def auth_logout_handler(request: web.Request) -> web.Response:
    """POST /api/auth/logout — clear session cookie."""
    response = web.json_response({"ok": True})
    clear_auth_cookie(response)
    return response


async def auth_me_handler(request: web.Request) -> web.Response:
    """GET /api/auth/me — return the current authenticated user."""
    user = await resolve_user(request)
    if not user:
        return web.json_response({"ok": False, "reason": "unauthorized"}, status=401)
    return web.json_response({"ok": True, "user": public_user(user)})


# ── Admin user management ─────────────────────────────────────────────────────


async def admin_list_users_handler(request: web.Request) -> web.Response:
    """GET /api/admin/users — list operator accounts (admin only)."""
    db = request.app["client_db"]
    users = [public_user(u) for u in await client_db.list_users(db)]
    return web.json_response({"ok": True, "users": users})


async def admin_approve_user_handler(request: web.Request) -> web.Response:
    """POST /api/admin/users/{user_id}/approve — approve a pending operator."""
    user_id = request.match_info["user_id"]
    db = request.app["client_db"]
    target = await client_db.get_user_by_id(db, user_id)
    if not target:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    if target["role"] == "admin":
        return web.json_response({"ok": False, "reason": "cannot_modify_admin"}, status=400)
    if target["status"] != "pending":
        return web.json_response({"ok": False, "reason": "not_pending"}, status=400)

    admin = request["user"]
    ok = await client_db.set_user_status(
        db, user_id, "approved", approved_by=admin["id"]
    )
    if not ok:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    updated = await client_db.get_user_by_id(db, user_id)
    return web.json_response({"ok": True, "user": public_user(updated)})


async def admin_reject_user_handler(request: web.Request) -> web.Response:
    """POST /api/admin/users/{user_id}/reject — reject a pending operator."""
    user_id = request.match_info["user_id"]
    db = request.app["client_db"]
    target = await client_db.get_user_by_id(db, user_id)
    if not target:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    if target["role"] == "admin":
        return web.json_response({"ok": False, "reason": "cannot_modify_admin"}, status=400)
    if target["status"] != "pending":
        return web.json_response({"ok": False, "reason": "not_pending"}, status=400)

    admin = request["user"]
    ok = await client_db.set_user_status(
        db, user_id, "rejected", approved_by=admin["id"]
    )
    if not ok:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    updated = await client_db.get_user_by_id(db, user_id)
    return web.json_response({"ok": True, "user": public_user(updated)})


async def admin_delete_user_handler(request: web.Request) -> web.Response:
    """DELETE /api/admin/users/{user_id} — remove an operator account."""
    user_id = request.match_info["user_id"]
    db = request.app["client_db"]
    target = await client_db.get_user_by_id(db, user_id)
    if not target:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    if target["role"] == "admin":
        return web.json_response({"ok": False, "reason": "cannot_delete_admin"}, status=400)

    ok = await client_db.delete_user(db, user_id)
    if not ok:
        return web.json_response({"ok": False, "reason": "not found"}, status=404)
    return web.json_response({"ok": True})


def create_app() -> web.Application:
    """Build and wire the aiohttp application and its routes."""

    app = web.Application(middlewares=[auth_middleware])
    session = ServerSession()
    app["session"] = session
    app.router.add_get("/", index_handler)
    app.router.add_get("/index.html", index_handler)
    app.router.add_get("/api/lan-ipv4", lan_ipv4_handler)
    app.router.add_get("/api/health", health_handler)
    app.router.add_get("/api/network-status", network_status_handler)
    app.router.add_get("/stream", debug_ws_handler)
    app.router.add_get(r"/stream/tts/{lang}", tts_stream_handler)
    app.router.add_get("/stream/text", text_stream_handler)
    # Control dashboard (English + Arabic)
    app.router.add_get("/control", control_page_handler)
    app.router.add_get("/control_ar", control_ar_page_handler)
    # Control REST API
    app.router.add_get("/api/control/{action}", control_handler)
    app.router.add_get("/api/browser/{action:.*}", browser_handler)
    app.router.add_get("/api/server/{action}", server_control_handler)
    # Client registry API
    app.router.add_post("/api/clients/register", client_register_handler)
    app.router.add_get("/api/clients", client_list_handler)
    app.router.add_post("/api/clients/{client_id}/rename", client_rename_handler)
    app.router.add_delete("/api/clients/{client_id}", client_delete_handler)
    # Operator authentication
    app.router.add_post("/api/auth/register", auth_register_handler)
    app.router.add_post("/api/auth/login", auth_login_handler)
    app.router.add_post("/api/auth/logout", auth_logout_handler)
    app.router.add_get("/api/auth/me", auth_me_handler)
    # Admin user management
    app.router.add_get("/api/admin/users", admin_list_users_handler)
    app.router.add_post("/api/admin/users/{user_id}/approve", admin_approve_user_handler)
    app.router.add_post("/api/admin/users/{user_id}/reject", admin_reject_user_handler)
    app.router.add_delete("/api/admin/users/{user_id}", admin_delete_user_handler)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app
