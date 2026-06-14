"""Tests for operator authentication and admin user management."""

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rtt_alhuda import db as client_db
from rtt_alhuda.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from rtt_alhuda.web_app import create_app


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(client_db, "DB_PATH", tmp_path / "test.db")


async def _start_app():
    app = create_app()
    client = TestClient(TestServer(app))
    await client.start_server()
    return app, client


async def _admin_token(client) -> str:
    resp = await client.post(
        "/api/auth/login",
        json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
    )
    assert resp.status == 200
    return (await resp.json())["token"]


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_startup_seeds_default_admin() -> None:
    app, client = await _start_app()
    try:
        db = app["client_db"]
        admin = await client_db.get_user_by_username(db, DEFAULT_ADMIN_USERNAME)
        assert admin
        assert admin["role"] == "admin"
        assert admin["status"] == "approved"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_creates_pending_operator() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/register",
            json={"username": "operator1", "password": "secret123"},
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["ok"] is True
        assert data["user"]["username"] == "operator1"
        assert data["user"]["status"] == "pending"
        assert data["user"]["role"] == "operator"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_rejects_duplicate_username() -> None:
    _, client = await _start_app()
    try:
        body = {"username": "dup_user", "password": "secret123"}
        assert (await client.post("/api/auth/register", json=body)).status == 201
        resp = await client.post("/api/auth/register", json=body)
        assert resp.status == 409
        assert (await resp.json())["reason"] == "username_taken"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_validates_password_length() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/register",
            json={"username": "shortpw", "password": "abc"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pending_operator_cannot_login() -> None:
    _, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "pending_op", "password": "secret123"},
        )
        resp = await client.post(
            "/api/auth/login",
            json={"username": "pending_op", "password": "secret123"},
        )
        assert resp.status == 403
        assert (await resp.json())["reason"] == "pending_approval"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_login_returns_token_and_cookie() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/login",
            json={"username": DEFAULT_ADMIN_USERNAME, "password": DEFAULT_ADMIN_PASSWORD},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["token"]
        assert data["user"]["role"] == "admin"
        assert resp.cookies.get("khutba_token")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_me_with_bearer_token() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.get("/api/auth/me", headers=_auth_headers(token))
        assert resp.status == 200
        data = await resp.json()
        assert data["user"]["username"] == DEFAULT_ADMIN_USERNAME
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_api_requires_auth() -> None:
    _, client = await _start_app()
    try:
        resp = await client.get("/api/control/status")
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_control_api_works_when_authenticated() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.get("/api/control/status", headers=_auth_headers(token))
        assert resp.status == 200
        assert "recording" in await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_register_stays_public() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/clients/register",
            json={"name": "TV", "screen_w": 1920, "screen_h": 1080},
        )
        assert resp.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_list_requires_auth() -> None:
    _, client = await _start_app()
    try:
        resp = await client.get("/api/clients")
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_approve_operator_flow() -> None:
    _, client = await _start_app()
    try:
        reg = await client.post(
            "/api/auth/register",
            json={"username": "new_op", "password": "secret123"},
        )
        user_id = (await reg.json())["user"]["id"]

        token = await _admin_token(client)
        headers = _auth_headers(token)

        approve = await client.post(f"/api/admin/users/{user_id}/approve", headers=headers)
        assert approve.status == 200
        assert (await approve.json())["user"]["status"] == "approved"

        login = await client.post(
            "/api/auth/login",
            json={"username": "new_op", "password": "secret123"},
        )
        assert login.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_list_users_requires_admin() -> None:
    app, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "not_admin", "password": "secret123"},
        )
        db = app["client_db"]
        op = await client_db.get_user_by_username(db, "not_admin")
        await client_db.set_user_status(db, op["id"], "approved")

        login = await client.post(
            "/api/auth/login",
            json={"username": "not_admin", "password": "secret123"},
        )
        op_token = (await login.json())["token"]

        resp = await client.get("/api/admin/users", headers=_auth_headers(op_token))
        assert resp.status == 403
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_cannot_delete_admin_user() -> None:
    app, client = await _start_app()
    try:
        token = await _admin_token(client)
        db = app["client_db"]
        admin = await client_db.get_user_by_username(db, DEFAULT_ADMIN_USERNAME)

        resp = await client.delete(
            f"/api/admin/users/{admin['id']}",
            headers=_auth_headers(token),
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_logout_clears_cookie() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.post("/api/auth/logout", headers=_auth_headers(token))
        assert resp.status == 200
        cookie = resp.cookies.get("khutba_token")
        if cookie is not None:
            assert cookie.value == ""
    finally:
        await client.close()
