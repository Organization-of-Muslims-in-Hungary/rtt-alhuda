"""Application wiring, session lifecycle, and decoupling tests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rtt_alhuda import config, db as client_db
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_app import create_app, start_recording, stop_recording
from rtt_alhuda.web_protocol import send_log, send_sse_control, send_transcription


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point DB_PATH to a fresh temp file so each test gets its own database."""
    monkeypatch.setattr(client_db, "DB_PATH", tmp_path / "test.db")


async def _admin_headers(client: TestClient) -> dict[str, str]:
    """Log in as the seeded default admin and return Authorization headers."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": config.DEFAULT_ADMIN_USERNAME, "password": config.DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status == 200
    token = (await resp.json())["token"]
    return {"Authorization": f"Bearer {token}"}


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
        headers = await _admin_headers(client)
        resp = await client.get("/api/control/status", headers=headers)
        assert resp.status == 200
        data = await resp.json()
        assert "recording" in data
        assert data["recording"] is False


@pytest.mark.asyncio
async def test_control_stop_when_not_recording() -> None:
    """Stopping when not recording returns a clear error, no crash."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get("/api/control/stop", headers=headers)
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


# ── Targeted SSE control tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_sse_control_broadcasts_to_all() -> None:
    """Without target_client_id, all SSE clients receive the message."""
    sse1 = AsyncMock()
    sse2 = AsyncMock()
    session = ServerSession()
    session.text_sse_clients = {sse1, sse2}

    await send_sse_control(session, "refresh")

    sse1.write.assert_called_once()
    sse2.write.assert_called_once()


@pytest.mark.asyncio
async def test_send_sse_control_targets_single_client() -> None:
    """With target_client_id, only that client's SSE gets the message."""
    sse_target = AsyncMock()
    sse_other = AsyncMock()
    session = ServerSession()
    session.text_sse_clients = {sse_target, sse_other}
    session.client_sse_map = {"client-1": {sse_target}, "client-2": {sse_other}}

    await send_sse_control(session, "navigate", target_client_id="client-1", page="tv")

    sse_target.write.assert_called_once()
    sse_other.write.assert_not_called()


@pytest.mark.asyncio
async def test_send_sse_control_target_missing_is_noop() -> None:
    """Targeting a non-existent client does nothing, no crash."""
    sse = AsyncMock()
    session = ServerSession()
    session.text_sse_clients = {sse}
    session.client_sse_map = {"client-1": {sse}}

    await send_sse_control(session, "refresh", target_client_id="no-such-client")

    sse.write.assert_not_called()


@pytest.mark.asyncio
async def test_send_sse_control_removes_broken_target() -> None:
    """A broken targeted SSE connection is cleaned up."""
    sse_broken = AsyncMock()
    sse_broken.write.side_effect = ConnectionResetError
    session = ServerSession()
    session.text_sse_clients = {sse_broken}
    session.client_sse_map = {"client-1": {sse_broken}}

    await send_sse_control(session, "refresh", target_client_id="client-1")

    assert "client-1" not in session.client_sse_map
    assert sse_broken not in session.text_sse_clients


@pytest.mark.asyncio
async def test_send_sse_control_broadcast_cleans_broken() -> None:
    """Broadcast removes broken SSE clients and their client_sse_map entry."""
    sse_ok = AsyncMock()
    sse_broken = AsyncMock()
    sse_broken.write.side_effect = OSError
    session = ServerSession()
    session.text_sse_clients = {sse_ok, sse_broken}
    session.client_sse_map = {"alive": {sse_ok}, "dead": {sse_broken}}

    await send_sse_control(session, "refresh")

    assert sse_broken not in session.text_sse_clients
    assert "dead" not in session.client_sse_map
    assert sse_ok in session.text_sse_clients
    assert "alive" in session.client_sse_map


# ── Multi-tab (duplicate client_id) SSE tests ───────────────────────────────


@pytest.mark.asyncio
async def test_multiple_tabs_coexist_in_client_sse_map() -> None:
    """Two SSE connections with the same client_id both stay in the map set."""
    session = ServerSession()
    tab_a = AsyncMock()
    tab_b = AsyncMock()
    cid = "same-client"

    # Both tabs connect (simulating setdefault().add())
    session.text_sse_clients.add(tab_a)
    session.client_sse_map.setdefault(cid, set()).add(tab_a)
    session.text_sse_clients.add(tab_b)
    session.client_sse_map.setdefault(cid, set()).add(tab_b)

    assert tab_a in session.client_sse_map[cid]
    assert tab_b in session.client_sse_map[cid]
    assert len(session.client_sse_map[cid]) == 2


@pytest.mark.asyncio
async def test_tab_disconnect_leaves_sibling_alive() -> None:
    """When one tab disconnects, the other tab's entry in the set survives."""
    session = ServerSession()
    tab_a = AsyncMock()
    tab_b = AsyncMock()
    cid = "same-client"

    session.text_sse_clients.update({tab_a, tab_b})
    session.client_sse_map[cid] = {tab_a, tab_b}

    # Tab B disconnects — simulate the finally block
    session.text_sse_clients.discard(tab_b)
    sse_set = session.client_sse_map.get(cid)
    sse_set.discard(tab_b)
    # set is non-empty so the key stays
    assert cid in session.client_sse_map
    assert tab_a in session.client_sse_map[cid]


@pytest.mark.asyncio
async def test_last_tab_disconnect_removes_map_key() -> None:
    """When the last tab for a client_id disconnects, the key is removed."""
    session = ServerSession()
    tab = AsyncMock()
    cid = "lonely"

    session.text_sse_clients.add(tab)
    session.client_sse_map[cid] = {tab}

    # Disconnect
    session.text_sse_clients.discard(tab)
    sse_set = session.client_sse_map.get(cid)
    sse_set.discard(tab)
    if not sse_set:
        del session.client_sse_map[cid]

    assert cid not in session.client_sse_map


@pytest.mark.asyncio
async def test_targeted_control_reaches_all_tabs() -> None:
    """Targeting a client_id sends the message to every tab in its set."""
    tab_a = AsyncMock()
    tab_b = AsyncMock()
    other = AsyncMock()
    session = ServerSession()
    session.text_sse_clients = {tab_a, tab_b, other}
    session.client_sse_map = {"client-1": {tab_a, tab_b}, "client-2": {other}}

    await send_sse_control(session, "refresh", target_client_id="client-1")

    tab_a.write.assert_called_once()
    tab_b.write.assert_called_once()
    other.write.assert_not_called()


@pytest.mark.asyncio
async def test_broken_tab_cleaned_without_affecting_sibling() -> None:
    """A broken tab in a multi-tab set is removed; the healthy sibling stays."""
    tab_ok = AsyncMock()
    tab_broken = AsyncMock()
    tab_broken.write.side_effect = ConnectionResetError
    session = ServerSession()
    session.text_sse_clients = {tab_ok, tab_broken}
    session.client_sse_map = {"client-1": {tab_ok, tab_broken}}

    await send_sse_control(session, "refresh", target_client_id="client-1")

    assert tab_broken not in session.text_sse_clients
    assert tab_broken not in session.client_sse_map["client-1"]
    assert tab_ok in session.client_sse_map["client-1"]
    assert "client-1" in session.client_sse_map


# ── Client SSE map on ServerSession ──────────────────────────────────────────


def test_session_has_client_sse_map() -> None:
    session = ServerSession()
    assert isinstance(session.client_sse_map, dict)
    assert len(session.client_sse_map) == 0


# ── Client API integration tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_register_creates_new() -> None:
    """POST /api/clients/register with no client_id creates a new client."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/clients/register",
            json={"name": "TestTV", "screen_w": 1920, "screen_h": 1080},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["client"]["name"] == "TestTV"
        assert data["client"]["device_type"] == "screen"
        assert len(data["client"]["id"]) == 12


@pytest.mark.asyncio
async def test_client_register_reidentifies() -> None:
    """Re-registering with the same client_id updates instead of duplicating."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        r1 = await client.post(
            "/api/clients/register",
            json={"name": "V1", "screen_w": 100, "screen_h": 100},
        )
        cid = (await r1.json())["client"]["id"]

        r2 = await client.post(
            "/api/clients/register",
            json={"client_id": cid, "name": "V2", "screen_w": 1920, "screen_h": 1080},
        )
        d2 = await r2.json()
        assert d2["client"]["id"] == cid
        assert d2["client"]["name"] == "V2"
        assert d2["client"]["device_type"] == "screen"


@pytest.mark.asyncio
async def test_client_list_empty() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get("/api/clients", headers=headers)
        data = await resp.json()
        assert data["ok"] is True
        assert data["clients"] == []


@pytest.mark.asyncio
async def test_client_list_shows_registered() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        await client.post(
            "/api/clients/register",
            json={"name": "A", "screen_w": 100, "screen_h": 100},
        )
        await client.post(
            "/api/clients/register",
            json={"name": "B", "screen_w": 1920, "screen_h": 1080},
        )
        resp = await client.get("/api/clients", headers=headers)
        data = await resp.json()
        assert len(data["clients"]) == 2
        names = {c["name"] for c in data["clients"]}
        assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_client_list_connected_field() -> None:
    """Clients in client_sse_map show connected=True, others False."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        r = await client.post(
            "/api/clients/register",
            json={"name": "Online", "screen_w": 100, "screen_h": 100},
        )
        cid = (await r.json())["client"]["id"]
        # Simulate SSE connection by putting a mock in client_sse_map
        app["session"].client_sse_map[cid] = {AsyncMock()}

        resp = await client.get("/api/clients", headers=headers)
        data = await resp.json()
        c = next(c for c in data["clients"] if c["id"] == cid)
        assert c["connected"] is True


@pytest.mark.asyncio
async def test_client_rename() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        r = await client.post(
            "/api/clients/register",
            json={"name": "Old", "screen_w": 100, "screen_h": 100},
        )
        cid = (await r.json())["client"]["id"]

        resp = await client.post(
            f"/api/clients/{cid}/rename", json={"name": "New"}, headers=headers
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        listing = await (await client.get("/api/clients", headers=headers)).json()
        assert listing["clients"][0]["name"] == "New"


@pytest.mark.asyncio
async def test_client_rename_nonexistent() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.post(
            "/api/clients/no-such/rename", json={"name": "X"}, headers=headers
        )
        assert resp.status == 404


@pytest.mark.asyncio
async def test_client_delete() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        r = await client.post(
            "/api/clients/register",
            json={"name": "Gone", "screen_w": 100, "screen_h": 100},
        )
        cid = (await r.json())["client"]["id"]

        resp = await client.delete(f"/api/clients/{cid}", headers=headers)
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

        listing = await (await client.get("/api/clients", headers=headers)).json()
        assert len(listing["clients"]) == 0


@pytest.mark.asyncio
async def test_client_delete_nonexistent() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.delete("/api/clients/no-such", headers=headers)
        assert resp.status == 404


# ── Browser targeting integration tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_navigate_with_target_param() -> None:
    """GET /api/browser/navigate/tv?target=X returns ok with the target echoed."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get(
            "/api/browser/navigate/tv?target=client-42", headers=headers
        )
        data = await resp.json()
        assert data["ok"] is True
        assert data["target"] == "client-42"


@pytest.mark.asyncio
async def test_browser_navigate_without_target() -> None:
    """Without ?target, the response has target=null (broadcast)."""
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get("/api/browser/navigate/app", headers=headers)
        data = await resp.json()
        assert data["ok"] is True
        assert data["target"] is None


@pytest.mark.asyncio
async def test_browser_refresh_with_target() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get("/api/browser/refresh?target=abc", headers=headers)
        data = await resp.json()
        assert data["ok"] is True
        assert data["target"] == "abc"


@pytest.mark.asyncio
async def test_browser_language_with_target() -> None:
    app = create_app()
    async with TestClient(TestServer(app)) as client:
        headers = await _admin_headers(client)
        resp = await client.get(
            "/api/browser/language/en?target=client-7", headers=headers
        )
        data = await resp.json()
        assert data["ok"] is True
        assert data["target"] == "client-7"
        assert data["lang"] == "en"

