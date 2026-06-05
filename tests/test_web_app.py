"""Application wiring, session lifecycle, and decoupling tests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_app import create_app, start_recording, stop_recording
from rtt_alhuda.web_protocol import send_log, send_transcription


# ── Route registration tests ────────────────────────────────────────────────


def test_create_app_registers_stream_route() -> None:
    app = create_app()
    get_paths = {
        r.resource.canonical
        for r in app.router.routes()
        if getattr(r, "method", None) == "GET"
    }
    assert "/stream" in get_paths
    assert "/stream/text" in get_paths
    assert "/stream/tts/{lang}" in get_paths
    assert "/api/lan-ipv4" in get_paths


def test_create_app_has_no_webrtc_routes() -> None:
    app = create_app()
    all_paths = {
        r.resource.canonical
        for r in app.router.routes()
    }
    assert "/webrtc/input" not in all_paths
    assert "/webrtc/tts" not in all_paths
    assert "/webrtc-test.html" not in all_paths


def test_create_app_has_server_session() -> None:
    app = create_app()
    assert "session" in app
    assert isinstance(app["session"], ServerSession)


# ── ServerSession independence tests ─────────────────────────────────────────


def test_session_created_without_ws() -> None:
    """ServerSession can be instantiated without any WebSocket."""
    session = ServerSession()
    assert session.recording is False
    assert len(session.debug_ws_clients) == 0
    assert len(session.text_sse_clients) == 0


@pytest.mark.asyncio
async def test_stop_recording_is_idempotent() -> None:
    """Calling stop_recording on an already-stopped session is safe."""
    session = ServerSession()
    await stop_recording(session)
    assert session.recording is False
    await stop_recording(session)  # second call must not raise
    assert session.recording is False


# ── Debug WS broadcast tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_log_broadcasts_to_multiple_debug_ws() -> None:
    """send_log should reach every connected debug WS client."""
    ws1 = AsyncMock()
    ws1.closed = False
    ws2 = AsyncMock()
    ws2.closed = False

    session = ServerSession()
    session.debug_ws_clients = {ws1, ws2}

    await send_log(session, "hello")

    ws1.send_str.assert_called_once()
    ws2.send_str.assert_called_once()


@pytest.mark.asyncio
async def test_send_log_skips_closed_ws() -> None:
    """Closed WS clients are silently removed, not sent to."""
    ws_alive = AsyncMock()
    ws_alive.closed = False
    ws_dead = AsyncMock()
    ws_dead.closed = True

    session = ServerSession()
    session.debug_ws_clients = {ws_alive, ws_dead}

    await send_log(session, "test")

    ws_alive.send_str.assert_called_once()
    ws_dead.send_str.assert_not_called()
    assert ws_dead not in session.debug_ws_clients


@pytest.mark.asyncio
async def test_send_log_removes_broken_ws() -> None:
    """A WS that raises on send is removed from the set."""
    ws_broken = AsyncMock()
    ws_broken.closed = False
    ws_broken.send_str.side_effect = ConnectionResetError

    session = ServerSession()
    session.debug_ws_clients = {ws_broken}

    await send_log(session, "test")

    assert ws_broken not in session.debug_ws_clients


@pytest.mark.asyncio
async def test_send_transcription_broadcasts() -> None:
    ws1 = AsyncMock()
    ws1.closed = False
    ws2 = AsyncMock()
    ws2.closed = False

    session = ServerSession()
    session.debug_ws_clients = {ws1, ws2}

    await send_transcription(session, {"type": "transcription", "ar": "test"})

    assert ws1.send_str.call_count == 1
    assert ws2.send_str.call_count == 1


@pytest.mark.asyncio
async def test_send_log_works_with_no_debug_clients() -> None:
    """send_log must not crash when no debug WS clients are connected."""
    session = ServerSession()
    await send_log(session, "nobody listening")  # must not raise


# ── SSE independence tests ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sse_set_persists_across_ws_disconnect() -> None:
    """SSE clients in the session must survive debug WS connect/disconnect."""
    session = ServerSession()
    sse_resp = AsyncMock()
    session.text_sse_clients.add(sse_resp)

    # Simulate a debug WS connecting and disconnecting.
    ws = AsyncMock()
    ws.closed = False
    session.debug_ws_clients.add(ws)
    session.debug_ws_clients.discard(ws)

    # SSE client must still be present.
    assert sse_resp in session.text_sse_clients


@pytest.mark.asyncio
async def test_multiple_sse_clients_receive_independently() -> None:
    """Each SSE response object is independent in the session set."""
    session = ServerSession()
    sse1 = AsyncMock()
    sse2 = AsyncMock()
    session.text_sse_clients.add(sse1)
    session.text_sse_clients.add(sse2)
    assert len(session.text_sse_clients) == 2

    # Remove one — the other stays.
    session.text_sse_clients.discard(sse1)
    assert sse2 in session.text_sse_clients
    assert len(session.text_sse_clients) == 1


# ── Control API independence tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_control_status_without_ws() -> None:
    """GET /api/control/status works even when no debug WS is connected."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/control/status")
        assert resp.status == 200
        data = await resp.json()
        assert "recording" in data
        assert data["recording"] is False


@pytest.mark.asyncio
async def test_control_stop_when_not_recording() -> None:
    """Stopping when not recording returns a clear error, no crash."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/control/stop")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is False
        assert data["reason"] == "not_recording"


@pytest.mark.asyncio
async def test_network_status_without_ws() -> None:
    """GET /api/network-status works with zero debug WS clients."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/network-status")
        assert resp.status == 200
        data = await resp.json()
        assert data["debug_ws_clients"] == 0
        assert data["ws_recording"] is False
        assert data["sse_clients"] == 0


# ── Recording lifecycle tests ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recording_state_independent_of_ws() -> None:
    """Recording flag on ServerSession is not tied to any WS."""
    session = ServerSession()
    assert session.recording is False
    session.recording = True
    assert session.recording is True
    # No WS involved at all — pure server state.


@pytest.mark.asyncio
async def test_debug_ws_disconnect_does_not_clear_subscribers() -> None:
    """Removing a WS from debug_ws_clients doesn't clear other subscribers."""
    ws1 = AsyncMock()
    ws2 = AsyncMock()
    session = ServerSession()
    session.debug_ws_clients = {ws1, ws2}
    session.mic_subscribers = {ws1, ws2}

    # ws1 disconnects
    session.debug_ws_clients.discard(ws1)
    session.mic_subscribers.discard(ws1)

    # ws2 is still subscribed
    assert ws2 in session.mic_subscribers
    assert ws2 in session.debug_ws_clients

