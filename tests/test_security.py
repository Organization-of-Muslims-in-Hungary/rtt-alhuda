"""Tests for security helpers (password hashing, JWT, public_user)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import jwt

from rtt_alhuda import config
from rtt_alhuda.security import (
    JWT_ALGORITHM,
    create_access_token,
    decode_access_token,
    hash_password,
    public_user,
    validate_password,
    validate_username,
    verify_password,
)


class TestValidateUsername:
    def test_valid_minimum_length(self) -> None:
        assert validate_username("abc") is None

    def test_valid_maximum_length(self) -> None:
        assert validate_username("a" * 32) is None

    def test_too_short_returns_error(self) -> None:
        assert validate_username("ab") is not None

    def test_special_chars_returns_error(self) -> None:
        assert validate_username("user@domain") is not None


class TestValidatePassword:
    def test_valid_exactly_min_length(self) -> None:
        from rtt_alhuda.config import MIN_PASSWORD_LENGTH

        assert validate_password("x" * MIN_PASSWORD_LENGTH) is None

    def test_too_short_returns_error(self) -> None:
        assert validate_password("short") is not None


class TestHashAndVerifyPassword:
    def test_correct_password_verifies(self) -> None:
        h = hash_password("mysecret")
        assert verify_password("mysecret", h) is True

    def test_wrong_password_does_not_verify(self) -> None:
        h = hash_password("mysecret")
        assert verify_password("wrongpassword", h) is False

    def test_invalid_hash_returns_false(self) -> None:
        assert verify_password("somepassword", "not-a-valid-bcrypt-hash") is False


class TestCreateAndDecodeAccessToken:
    def _sample_user(self) -> dict:
        return {
            "id": "user-id-123",
            "org_id": "org-id-456",
            "username": "testop",
            "role": "operator",
        }

    def test_roundtrip_decode(self) -> None:
        token = create_access_token(self._sample_user())
        claims = decode_access_token(token)
        assert claims is not None
        assert claims["sub"] == "user-id-123"
        assert claims["org_id"] == "org-id-456"
        assert claims["role"] == "operator"

    def test_decode_expired_token_returns_none(self) -> None:
        now = int(time.time())
        payload = {
            "sub": "user-id",
            "org_id": "org-id",
            "username": "u",
            "role": "operator",
            "iat": now - 10000,
            "exp": now - 1,
        }
        expired_token = jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)
        assert decode_access_token(expired_token) is None

    def test_decode_malformed_token_returns_none(self) -> None:
        assert decode_access_token("not.a.jwt") is None


class TestPublicUser:
    def _user(self) -> SimpleNamespace:
        return SimpleNamespace(
            id="abc-123",
            org_id="org-1",
            email="op@example.com",
            username="operator",
            role=SimpleNamespace(value="operator"),
            status=SimpleNamespace(value="active"),
            created_at=datetime.now(timezone.utc),
            approved_at=None,
        )

    def test_excludes_password_hash(self) -> None:
        result = public_user(self._user())
        assert "password_hash" not in result

    def test_includes_required_fields(self) -> None:
        result = public_user(self._user())
        assert result["id"] == "abc-123"
        assert result["org_id"] == "org-1"
        assert result["email"] == "op@example.com"
        assert result["username"] == "operator"
        assert result["role"] == "operator"
        assert result["status"] == "active"
