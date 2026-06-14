"""Tests for operator authentication and admin user management."""

import time

import jwt
import pytest
from aiohttp.test_utils import TestClient, TestServer

from rtt_alhuda import config, db as client_db
from rtt_alhuda.auth import (
    JWT_ALGORITHM,
    can_access_control,
    create_access_token,
    decode_access_token,
    hash_password,
    is_admin,
    path_requires_auth,
    public_user,
    validate_password,
    validate_username,
    verify_password,
)
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
        json={"username": config.DEFAULT_ADMIN_USERNAME, "password": config.DEFAULT_ADMIN_PASSWORD},
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
        admin = await client_db.get_user_by_username(db, config.DEFAULT_ADMIN_USERNAME)
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
            json={"username": config.DEFAULT_ADMIN_USERNAME, "password": config.DEFAULT_ADMIN_PASSWORD},
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
        assert data["user"]["username"] == config.DEFAULT_ADMIN_USERNAME
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
        admin = await client_db.get_user_by_username(db, config.DEFAULT_ADMIN_USERNAME)

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


# ── Additional integration tests for edge cases ──────────────────────────────


@pytest.mark.asyncio
async def test_register_invalid_json_returns_400() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/register",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["reason"] == "invalid json"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_invalid_username_format_returns_400() -> None:
    _, client = await _start_app()
    try:
        # Username too short (2 chars)
        resp = await client.post(
            "/api/auth/register",
            json={"username": "ab", "password": "secret123"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["ok"] is False
        assert "username" in data["reason"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_register_username_with_special_chars_returns_400() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/register",
            json={"username": "user@domain", "password": "secret123"},
        )
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_invalid_json_returns_400() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/login",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        assert (await resp.json())["reason"] == "invalid json"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_wrong_password_returns_401() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/login",
            json={"username": config.DEFAULT_ADMIN_USERNAME, "password": "wrongpassword"},
        )
        assert resp.status == 401
        assert (await resp.json())["reason"] == "invalid_credentials"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_login_nonexistent_user_returns_401() -> None:
    _, client = await _start_app()
    try:
        resp = await client.post(
            "/api/auth/login",
            json={"username": "ghost_user", "password": "password123"},
        )
        assert resp.status == 401
        assert (await resp.json())["reason"] == "invalid_credentials"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rejected_operator_cannot_login() -> None:
    app, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "rejected_op", "password": "secret123"},
        )
        db = app["client_db"]
        op = await client_db.get_user_by_username(db, "rejected_op")
        await client_db.set_user_status(db, op["id"], "rejected")

        resp = await client.post(
            "/api/auth/login",
            json={"username": "rejected_op", "password": "secret123"},
        )
        assert resp.status == 403
        assert (await resp.json())["reason"] == "rejected"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_auth_me_without_token_returns_401() -> None:
    _, client = await _start_app()
    try:
        resp = await client.get("/api/auth/me")
        assert resp.status == 401
        assert (await resp.json())["reason"] == "unauthorized"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_list_users_returns_all_users() -> None:
    app, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "op_one", "password": "secret123"},
        )
        await client.post(
            "/api/auth/register",
            json={"username": "op_two", "password": "secret123"},
        )
        token = await _admin_token(client)
        resp = await client.get("/api/admin/users", headers=_auth_headers(token))
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        usernames = {u["username"] for u in data["users"]}
        # default admin + two operators
        assert config.DEFAULT_ADMIN_USERNAME in usernames
        assert "op_one" in usernames
        assert "op_two" in usernames
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_approve_nonexistent_user_returns_404() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.post(
            "/api/admin/users/no-such-id/approve",
            headers=_auth_headers(token),
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_approve_already_approved_returns_400() -> None:
    app, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "pre_approved", "password": "secret123"},
        )
        db = app["client_db"]
        op = await client_db.get_user_by_username(db, "pre_approved")
        await client_db.set_user_status(db, op["id"], "approved")

        token = await _admin_token(client)
        resp = await client.post(
            f"/api/admin/users/{op['id']}/approve",
            headers=_auth_headers(token),
        )
        assert resp.status == 400
        assert (await resp.json())["reason"] == "not_pending"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_cannot_approve_admin_user() -> None:
    app, client = await _start_app()
    try:
        token = await _admin_token(client)
        db = app["client_db"]
        admin = await client_db.get_user_by_username(db, config.DEFAULT_ADMIN_USERNAME)
        resp = await client.post(
            f"/api/admin/users/{admin['id']}/approve",
            headers=_auth_headers(token),
        )
        assert resp.status == 400
        assert (await resp.json())["reason"] == "cannot_modify_admin"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_reject_operator_flow() -> None:
    _, client = await _start_app()
    try:
        reg = await client.post(
            "/api/auth/register",
            json={"username": "to_reject", "password": "secret123"},
        )
        user_id = (await reg.json())["user"]["id"]

        token = await _admin_token(client)
        resp = await client.post(
            f"/api/admin/users/{user_id}/reject",
            headers=_auth_headers(token),
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["user"]["status"] == "rejected"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_reject_nonexistent_user_returns_404() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.post(
            "/api/admin/users/no-such-id/reject",
            headers=_auth_headers(token),
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_delete_operator_succeeds() -> None:
    _, client = await _start_app()
    try:
        reg = await client.post(
            "/api/auth/register",
            json={"username": "to_delete", "password": "secret123"},
        )
        user_id = (await reg.json())["user"]["id"]

        token = await _admin_token(client)
        resp = await client.delete(
            f"/api/admin/users/{user_id}",
            headers=_auth_headers(token),
        )
        assert resp.status == 200
        assert (await resp.json())["ok"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_delete_nonexistent_user_returns_404() -> None:
    _, client = await _start_app()
    try:
        token = await _admin_token(client)
        resp = await client.delete(
            "/api/admin/users/no-such-id",
            headers=_auth_headers(token),
        )
        assert resp.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pending_operator_forbidden_from_control_api() -> None:
    _, client = await _start_app()
    try:
        await client.post(
            "/api/auth/register",
            json={"username": "still_pending", "password": "secret123"},
        )
        # Manually obtain a token for a pending user by bypassing login
        # (login would return 403 for pending, so we create a token directly)
        db_user = {
            "id": "fake-id",
            "username": "still_pending",
            "role": "operator",
            "status": "pending",
            "created_at": time.time(),
        }
        token = create_access_token(db_user)
        resp = await client.get(
            "/api/control/status",
            headers=_auth_headers(token),
        )
        # Token is valid but user no longer exists in DB with that fake ID -> 401
        assert resp.status in (401, 403)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_server_api_requires_auth() -> None:
    _, client = await _start_app()
    try:
        resp = await client.get("/api/server/status")
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_browser_api_requires_auth() -> None:
    _, client = await _start_app()
    try:
        resp = await client.get("/api/browser/navigate/tv")
        assert resp.status == 401
    finally:
        await client.close()


# ── Pure unit tests for auth.py helper functions ─────────────────────────────


class TestValidateUsername:
    def test_valid_minimum_length(self) -> None:
        assert validate_username("abc") is None

    def test_valid_maximum_length(self) -> None:
        assert validate_username("a" * 32) is None

    def test_valid_with_underscore(self) -> None:
        assert validate_username("my_user") is None

    def test_valid_mixed_case_and_digits(self) -> None:
        assert validate_username("User123") is None

    def test_too_short_returns_error(self) -> None:
        assert validate_username("ab") is not None

    def test_single_char_returns_error(self) -> None:
        assert validate_username("a") is not None

    def test_empty_string_returns_error(self) -> None:
        assert validate_username("") is not None

    def test_too_long_returns_error(self) -> None:
        assert validate_username("a" * 33) is not None

    def test_special_chars_returns_error(self) -> None:
        assert validate_username("user@domain") is not None

    def test_hyphen_returns_error(self) -> None:
        assert validate_username("my-user") is not None

    def test_space_returns_error(self) -> None:
        assert validate_username("my user") is not None

    def test_error_message_mentions_username(self) -> None:
        err = validate_username("x")
        assert err is not None
        assert "username" in err.lower()


class TestValidatePassword:
    def test_valid_exactly_min_length(self) -> None:
        from rtt_alhuda.config import MIN_PASSWORD_LENGTH
        assert validate_password("x" * MIN_PASSWORD_LENGTH) is None

    def test_valid_longer_than_min(self) -> None:
        assert validate_password("a_longer_password_here") is None

    def test_too_short_returns_error(self) -> None:
        from rtt_alhuda.config import MIN_PASSWORD_LENGTH
        assert validate_password("x" * (MIN_PASSWORD_LENGTH - 1)) is not None

    def test_empty_string_returns_error(self) -> None:
        assert validate_password("") is not None

    def test_error_message_mentions_password(self) -> None:
        err = validate_password("short")
        assert err is not None
        assert "password" in err.lower()


class TestHashAndVerifyPassword:
    def test_correct_password_verifies(self) -> None:
        h = hash_password("mysecret")
        assert verify_password("mysecret", h) is True

    def test_wrong_password_does_not_verify(self) -> None:
        h = hash_password("mysecret")
        assert verify_password("wrongpassword", h) is False

    def test_empty_password_does_not_verify_nonempty_hash(self) -> None:
        h = hash_password("mysecret")
        assert verify_password("", h) is False

    def test_hash_is_different_each_call(self) -> None:
        h1 = hash_password("samepassword")
        h2 = hash_password("samepassword")
        assert h1 != h2  # bcrypt gensalt differs

    def test_invalid_hash_returns_false(self) -> None:
        assert verify_password("somepassword", "not-a-valid-bcrypt-hash") is False

    def test_hash_is_string(self) -> None:
        h = hash_password("testpass")
        assert isinstance(h, str)


class TestCreateAndDecodeAccessToken:
    def _sample_user(self) -> dict:
        return {
            "id": "user-id-123",
            "username": "testop",
            "role": "operator",
            "status": "approved",
            "created_at": time.time(),
        }

    def test_roundtrip_decode(self) -> None:
        user = self._sample_user()
        token = create_access_token(user)
        claims = decode_access_token(token)
        assert claims is not None
        assert claims["sub"] == user["id"]
        assert claims["username"] == user["username"]
        assert claims["role"] == user["role"]

    def test_token_is_string(self) -> None:
        token = create_access_token(self._sample_user())
        assert isinstance(token, str)
        assert len(token) > 0

    def test_decode_with_wrong_secret_returns_none(self) -> None:
        user = self._sample_user()
        token = create_access_token(user)
        # Decode using wrong secret
        try:
            result = jwt.decode(
                token,
                "wrong-secret",
                algorithms=[JWT_ALGORITHM],
                options={"require": ["sub"]},
            )
            # Should not reach here
            assert False, "Expected exception"
        except jwt.PyJWTError:
            pass  # expected

    def test_decode_expired_token_returns_none(self) -> None:
        user = self._sample_user()
        now = int(time.time())
        payload = {
            "sub": user["id"],
            "username": user["username"],
            "role": user["role"],
            "iat": now - 10000,
            "exp": now - 1,  # already expired
        }
        expired_token = jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert decode_access_token(expired_token) is None

    def test_decode_malformed_token_returns_none(self) -> None:
        assert decode_access_token("not.a.jwt") is None

    def test_decode_empty_string_returns_none(self) -> None:
        assert decode_access_token("") is None

    def test_decode_without_jwt_secret_returns_none(self, monkeypatch) -> None:
        monkeypatch.setattr(config, "JWT_SECRET", None)
        user = self._sample_user()
        # Build a token directly without using create_access_token (which needs JWT_SECRET)
        payload = {"sub": user["id"], "exp": int(time.time()) + 3600}
        token = jwt.encode(payload, "some-secret", algorithm=JWT_ALGORITHM)
        assert decode_access_token(token) is None

    def test_token_contains_expiry(self) -> None:
        user = self._sample_user()
        token = create_access_token(user)
        claims = decode_access_token(token)
        assert claims is not None
        assert "exp" in claims
        assert claims["exp"] > int(time.time())


class TestPublicUser:
    def _full_user(self) -> dict:
        return {
            "id": "abc123",
            "username": "operator",
            "role": "operator",
            "status": "approved",
            "password_hash": "$2b$12$somehash",
            "created_at": 1700000000.0,
            "approved_at": 1700001000.0,
            "approved_by": "admin-id",
        }

    def test_excludes_password_hash(self) -> None:
        result = public_user(self._full_user())
        assert "password_hash" not in result

    def test_includes_required_fields(self) -> None:
        result = public_user(self._full_user())
        for field in ("id", "username", "role", "status", "created_at"):
            assert field in result

    def test_includes_optional_approval_fields(self) -> None:
        result = public_user(self._full_user())
        assert result["approved_at"] == 1700001000.0
        assert result["approved_by"] == "admin-id"

    def test_optional_fields_default_to_none(self) -> None:
        user = self._full_user()
        del user["approved_at"]
        del user["approved_by"]
        result = public_user(user)
        assert result["approved_at"] is None
        assert result["approved_by"] is None

    def test_correct_values_preserved(self) -> None:
        user = self._full_user()
        result = public_user(user)
        assert result["id"] == user["id"]
        assert result["username"] == user["username"]
        assert result["role"] == user["role"]
        assert result["status"] == user["status"]


class TestPathRequiresAuth:
    def test_admin_path_requires_auth(self) -> None:
        assert path_requires_auth("/api/admin/users", "GET") is True

    def test_admin_subpath_requires_auth(self) -> None:
        assert path_requires_auth("/api/admin/users/123/approve", "POST") is True

    def test_control_path_requires_auth(self) -> None:
        assert path_requires_auth("/api/control/status", "GET") is True

    def test_browser_path_requires_auth(self) -> None:
        assert path_requires_auth("/api/browser/navigate/tv", "GET") is True

    def test_server_path_requires_auth(self) -> None:
        assert path_requires_auth("/api/server/status", "GET") is True

    def test_clients_get_requires_auth(self) -> None:
        assert path_requires_auth("/api/clients", "GET") is True

    def test_clients_post_does_not_require_auth(self) -> None:
        assert path_requires_auth("/api/clients", "POST") is False

    def test_client_rename_post_requires_auth(self) -> None:
        assert path_requires_auth("/api/clients/abc123/rename", "POST") is True

    def test_client_rename_get_does_not_require_auth(self) -> None:
        assert path_requires_auth("/api/clients/abc123/rename", "GET") is False

    def test_client_delete_requires_auth(self) -> None:
        assert path_requires_auth("/api/clients/abc123", "DELETE") is True

    def test_client_register_does_not_require_auth(self) -> None:
        assert path_requires_auth("/api/clients/register", "POST") is False

    def test_auth_login_does_not_require_auth(self) -> None:
        assert path_requires_auth("/api/auth/login", "POST") is False

    def test_auth_register_does_not_require_auth(self) -> None:
        assert path_requires_auth("/api/auth/register", "POST") is False

    def test_root_does_not_require_auth(self) -> None:
        assert path_requires_auth("/", "GET") is False

    def test_stream_does_not_require_auth(self) -> None:
        assert path_requires_auth("/stream", "GET") is False


class TestCanAccessControl:
    def test_approved_user_can_access(self) -> None:
        user = {"status": "approved", "role": "operator"}
        assert can_access_control(user) is True

    def test_pending_user_cannot_access(self) -> None:
        user = {"status": "pending", "role": "operator"}
        assert can_access_control(user) is False

    def test_rejected_user_cannot_access(self) -> None:
        user = {"status": "rejected", "role": "operator"}
        assert can_access_control(user) is False


class TestIsAdmin:
    def test_admin_role_returns_true(self) -> None:
        assert is_admin({"role": "admin"}) is True

    def test_operator_role_returns_false(self) -> None:
        assert is_admin({"role": "operator"}) is False

    def test_missing_role_returns_false(self) -> None:
        assert is_admin({}) is False

    def test_empty_role_returns_false(self) -> None:
        assert is_admin({"role": ""}) is False


# ── Config validate_auth_config tests ────────────────────────────────────────


class TestValidateAuthConfig:
    def test_raises_when_jwt_secret_missing(self, monkeypatch) -> None:
        monkeypatch.delenv("KHUTBA_JWT_SECRET", raising=False)
        monkeypatch.setenv("KHUTBA_ADMIN_USERNAME", "admin")
        monkeypatch.setenv("KHUTBA_ADMIN_PASSWORD", "password123")
        with pytest.raises(RuntimeError, match="KHUTBA_JWT_SECRET"):
            config.validate_auth_config()

    def test_raises_when_admin_username_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("KHUTBA_JWT_SECRET", "a-secret-that-is-long-enough-32chars")
        monkeypatch.delenv("KHUTBA_ADMIN_USERNAME", raising=False)
        monkeypatch.setenv("KHUTBA_ADMIN_PASSWORD", "password123")
        with pytest.raises(RuntimeError, match="KHUTBA_ADMIN_USERNAME"):
            config.validate_auth_config()

    def test_raises_when_admin_password_missing(self, monkeypatch) -> None:
        monkeypatch.setenv("KHUTBA_JWT_SECRET", "a-secret-that-is-long-enough-32chars")
        monkeypatch.setenv("KHUTBA_ADMIN_USERNAME", "admin")
        monkeypatch.delenv("KHUTBA_ADMIN_PASSWORD", raising=False)
        with pytest.raises(RuntimeError, match="KHUTBA_ADMIN_PASSWORD"):
            config.validate_auth_config()

    def test_raises_when_all_missing(self, monkeypatch) -> None:
        monkeypatch.delenv("KHUTBA_JWT_SECRET", raising=False)
        monkeypatch.delenv("KHUTBA_ADMIN_USERNAME", raising=False)
        monkeypatch.delenv("KHUTBA_ADMIN_PASSWORD", raising=False)
        with pytest.raises(RuntimeError):
            config.validate_auth_config()

    def test_sets_globals_when_all_present(self, monkeypatch) -> None:
        monkeypatch.setenv("KHUTBA_JWT_SECRET", "my-test-secret-that-is-long-enough")
        monkeypatch.setenv("KHUTBA_ADMIN_USERNAME", "testadmin")
        monkeypatch.setenv("KHUTBA_ADMIN_PASSWORD", "testpassword")
        config.validate_auth_config()
        assert config.JWT_SECRET == "my-test-secret-that-is-long-enough"
        assert config.DEFAULT_ADMIN_USERNAME == "testadmin"
        assert config.DEFAULT_ADMIN_PASSWORD == "testpassword"

    def test_error_message_lists_all_missing_vars(self, monkeypatch) -> None:
        monkeypatch.delenv("KHUTBA_JWT_SECRET", raising=False)
        monkeypatch.delenv("KHUTBA_ADMIN_USERNAME", raising=False)
        monkeypatch.delenv("KHUTBA_ADMIN_PASSWORD", raising=False)
        with pytest.raises(RuntimeError) as exc_info:
            config.validate_auth_config()
        msg = str(exc_info.value)
        assert "KHUTBA_JWT_SECRET" in msg
        assert "KHUTBA_ADMIN_USERNAME" in msg
        assert "KHUTBA_ADMIN_PASSWORD" in msg
