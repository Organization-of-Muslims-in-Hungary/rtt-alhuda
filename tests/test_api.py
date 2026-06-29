"""Integration tests for the FastAPI service."""

from __future__ import annotations

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from rtt_alhuda import config


@pytest.fixture
async def app():
    from rtt_alhuda.app import create_app

    application = create_app()
    async with LifespanManager(application):
        yield application


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _superadmin_token(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": config.DEFAULT_ADMIN_EMAIL,
            "password": config.DEFAULT_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "healthy"}


@pytest.mark.asyncio
async def test_register_disabled(client: AsyncClient):
    resp = await client.post(
        "/api/auth/register",
        json={"email": "someone@example.com", "password": "secret123"},
    )
    assert resp.status_code == 403
    assert resp.json()["reason"] == "registration_disabled"


@pytest.mark.asyncio
async def test_login_superadmin(client: AsyncClient):
    resp = await client.post(
        "/api/auth/login",
        json={
            "email": config.DEFAULT_ADMIN_EMAIL,
            "password": config.DEFAULT_ADMIN_PASSWORD,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["token"]
    assert data["user"]["role"] == "superadmin"


@pytest.mark.asyncio
async def test_auth_me(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.get("/api/auth/me", headers=_auth_headers(token))
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == config.DEFAULT_ADMIN_EMAIL


@pytest.mark.asyncio
async def test_auth_me_without_token(client: AsyncClient):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_control_status_requires_auth(client: AsyncClient):
    resp = await client.get("/api/orgs/default/control/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_control_status_works(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.get(
        "/api/orgs/default/control/status", headers=_auth_headers(token)
    )
    assert resp.status_code == 200
    assert "recording" in resp.json()


@pytest.mark.asyncio
async def test_control_start_remote_then_stop(client: AsyncClient):
    token = await _superadmin_token(client)
    start = await client.get(
        "/api/orgs/default/control/start",
        params={"source": "remote"},
        headers=_auth_headers(token),
    )
    assert start.status_code == 200
    assert start.json()["ok"] is True
    stop = await client.get(
        "/api/orgs/default/control/stop", headers=_auth_headers(token)
    )
    assert stop.status_code == 200
    assert stop.json()["ok"] is True


@pytest.mark.asyncio
async def test_control_invalid_source(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.get(
        "/api/orgs/default/control/start",
        params={"source": "bluetooth"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_devices_register_public(client: AsyncClient):
    resp = await client.post(
        "/api/orgs/default/devices",
        json={"name": "TV", "screen_w": 1920, "screen_h": 1080},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["client"]["device_type"] == "screen"
    assert data["client"]["id"]


@pytest.mark.asyncio
async def test_devices_list_requires_auth(client: AsyncClient):
    resp = await client.get("/api/orgs/default/devices")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_devices_list_works(client: AsyncClient):
    token = await _superadmin_token(client)
    await client.post(
        "/api/orgs/default/devices",
        json={"name": "A", "screen_w": 100, "screen_h": 100},
    )
    resp = await client.get(
        "/api/orgs/default/devices", headers=_auth_headers(token)
    )
    assert resp.status_code == 200
    assert len(resp.json()["clients"]) == 1


@pytest.mark.asyncio
async def test_admin_create_org(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.post(
        "/api/admin/orgs",
        json={"name": "Alhuda", "slug": "alhuda"},
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["org"]["slug"] == "alhuda"


@pytest.mark.asyncio
async def test_admin_create_user(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.post(
        "/api/admin/orgs/default/users",
        json={
            "email": "op@example.com",
            "password": "secret123",
            "role": "operator",
        },
        headers=_auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "op@example.com"
    assert resp.json()["user"]["status"] == "active"


@pytest.mark.asyncio
async def test_admin_list_users(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.get(
        "/api/admin/orgs/default/users", headers=_auth_headers(token)
    )
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()["users"]}
    assert config.DEFAULT_ADMIN_EMAIL in emails


@pytest.mark.asyncio
async def test_network_status_requires_auth(client: AsyncClient):
    resp = await client.get("/api/orgs/default/network-status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_browser_navigate_requires_auth(client: AsyncClient):
    resp = await client.get("/api/orgs/default/browser/navigate/tv")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_server_status_requires_auth(client: AsyncClient):
    resp = await client.get("/api/orgs/default/server/status")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_org_404(client: AsyncClient):
    token = await _superadmin_token(client)
    resp = await client.get(
        "/api/orgs/no-such-org/control/status", headers=_auth_headers(token)
    )
    assert resp.status_code == 404
