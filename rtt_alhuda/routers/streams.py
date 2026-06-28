"""Public SSE and WebSocket streams (org-scoped, no auth)."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from rtt_alhuda.audio_capture import feed_remote_audio, stop_recording
from rtt_alhuda.config import CHANNELS, SAMPLE_WIDTH_BYTES
from rtt_alhuda.database import get_session_factory
from rtt_alhuda.db_models import Organization
from rtt_alhuda.device_service import register_device, touch_device
from rtt_alhuda.session_manager import SessionManager
from rtt_alhuda.sse_channel import SseChannel
from rtt_alhuda.web_protocol import is_ws_closed, send_log

router = APIRouter()


async def _get_org_by_slug(org_slug: str) -> Organization | None:
    factory = get_session_factory()
    async with factory() as db:
        result = await db.execute(
            select(Organization).where(Organization.slug == org_slug)
        )
        org = result.scalar_one_or_none()
        if org is not None:
            await db.refresh(org)
        return org


def _get_session_manager(request_or_ws) -> SessionManager:
    return request_or_ws.app.state.session_manager


# ── SSE text stream ───────────────────────────────────────────────────────────


@router.get("/{org_slug}/stream/text")
async def text_stream(
    org_slug: str,
    request: Request,
    client_id: str | None = None,
    name: str = "",
    screen_w: int = 0,
    screen_h: int = 0,
) -> StreamingResponse:
    """Serve transcription/translation updates via Server-Sent Events.

    Auto-registers the connecting device and emits an ``event: registered``
    message so the frontend can persist the device id.
    """
    org = await _get_org_by_slug(org_slug)
    if org is None:
        return StreamingResponse(
            iter([b"event: error\ndata: organization not found\n\n"]),
            media_type="text/event-stream",
            status_code=404,
        )

    session_manager = _get_session_manager(request)
    session = await session_manager.get_or_create(org.id)

    ua = request.headers.get("User-Agent", "")
    factory = get_session_factory()
    async with factory() as db:
        device = await register_device(
            db, org.id, client_id, name, screen_w, screen_h, ua
        )
    device_id = device["id"]

    channel = SseChannel()
    session.text_sse_clients.add(channel)
    session.client_sse_map.setdefault(device_id, set()).add(channel)

    reg_msg = (
        f"event: registered\ndata: {json.dumps(device, ensure_ascii=False)}\n\n".encode()
    )
    await channel.write(reg_msg)

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(channel.queue.get(), timeout=5)
                    yield data
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
                    async with factory() as db:
                        await touch_device(db, device_id)
        except asyncio.CancelledError:
            pass
        finally:
            session.text_sse_clients.discard(channel)
            sse_set = session.client_sse_map.get(device_id)
            if sse_set is not None:
                sse_set.discard(channel)
                if not sse_set:
                    del session.client_sse_map[device_id]

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ── Debug / remote-mic WebSocket ──────────────────────────────────────────────


@router.websocket("/{org_slug}/stream")
async def debug_ws(websocket: WebSocket, org_slug: str) -> None:
    """Debug WebSocket: observe logs/transcriptions, optionally subscribe to audio.

    Also accepts remote-mic PCM frames prefixed with ``0x03``.
    """
    org = await _get_org_by_slug(org_slug)
    if org is None:
        await websocket.accept()
        await websocket.close(code=1000, reason="organization not found")
        return

    await websocket.accept()
    session_manager = _get_session_manager(websocket)
    session = await session_manager.get_or_create(org.id)
    session.debug_ws_clients.add(websocket)

    await send_log(session, "Debug WebSocket connected")
    print("Debug WebSocket client connected")

    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] != "websocket.receive":
                continue

            if "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    await send_log(session, "Invalid JSON message", "warn")
                    continue

                msg_type = payload.get("type")
                if msg_type == "subscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        session.mic_subscribers.add(websocket)
                    elif stream == "tts":
                        session.tts_subscribers.add(websocket)
                    else:
                        await send_log(session, f"Unknown stream: {stream}", "warn")
                elif msg_type == "unsubscribe":
                    stream = payload.get("stream")
                    if stream == "mic":
                        session.mic_subscribers.discard(websocket)
                    elif stream == "tts":
                        session.tts_subscribers.discard(websocket)
                    else:
                        await send_log(session, f"Unknown stream: {stream}", "warn")
                else:
                    await send_log(
                        session, f"Unknown message type: {msg_type}", "warn"
                    )

            elif "bytes" in msg and msg["bytes"] is not None:
                data = msg["bytes"]
                if isinstance(data, (bytes, bytearray)) and len(data) > 1:
                    prefix_byte = data[0]
                    if prefix_byte == 0x03 and session.recording:
                        pcm_payload = bytes(data[1:])
                        bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES
                        if (
                            len(pcm_payload) == 0
                            or len(pcm_payload) % bytes_per_frame != 0
                        ):
                            continue
                        if session.remote_mic_ws is None:
                            session.remote_mic_ws = websocket
                        if session.remote_mic_ws is not websocket:
                            continue
                        await feed_remote_audio(session, pcm_payload)
    except WebSocketDisconnect:
        pass
    finally:
        session.debug_ws_clients.discard(websocket)
        session.mic_subscribers.discard(websocket)
        session.tts_subscribers.discard(websocket)
        if session.remote_mic_ws is websocket:
            session.remote_mic_ws = None
            if session.recording and session.audio_source == "remote":
                await stop_recording(session)
                await send_log(
                    session,
                    "Remote mic WS disconnected — recording stopped",
                    "warn",
                )
        print("Debug WebSocket client disconnected")


# ── TTS / live-arabic satellite WebSocket ─────────────────────────────────────


@router.websocket("/{org_slug}/stream/tts/{lang}")
async def tts_stream(websocket: WebSocket, org_slug: str, lang: str) -> None:
    """WebSocket: ``en``/``hu`` -> MP3 (0x02). ``ar`` -> live mic PCM (0x01)."""
    lang = (lang or "").lower()
    if lang not in ("ar", "en", "hu"):
        await websocket.accept()
        await websocket.close(code=1000, reason="lang must be ar, en, or hu")
        return

    org = await _get_org_by_slug(org_slug)
    if org is None:
        await websocket.accept()
        await websocket.close(code=1000, reason="organization not found")
        return

    await websocket.accept()
    session_manager = _get_session_manager(websocket)
    session = await session_manager.get_or_create(org.id)

    # Wait until the session is recording (or the satellite disconnects).
    while not is_ws_closed(websocket):
        if session.recording and (lang == "ar" or session.tts_queues is not None):
            break
        await asyncio.sleep(0.2)

    if is_ws_closed(websocket):
        return

    async with session.lock:
        if lang == "ar":
            session.original_audio_satellites.add(websocket)
        else:
            session.tts_satellites[lang].add(websocket)

    try:
        while not is_ws_closed(websocket):
            try:
                await asyncio.wait_for(websocket.receive(), timeout=20)
            except asyncio.TimeoutError:
                if is_ws_closed(websocket):
                    break
            except WebSocketDisconnect:
                break
    finally:
        async with session.lock:
            if lang == "ar":
                session.original_audio_satellites.discard(websocket)
            else:
                session.tts_satellites[lang].discard(websocket)
        if not is_ws_closed(websocket):
            await websocket.close()
