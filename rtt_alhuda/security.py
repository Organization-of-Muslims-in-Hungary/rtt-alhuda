"""Password hashing, JWT tokens, and auth helpers (FastAPI-native)."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import bcrypt
import jwt
from fastapi import Request

from rtt_alhuda import config
from rtt_alhuda.config import (
    JWT_COOKIE_NAME,
    JWT_EXPIRY_SECONDS,
    MIN_PASSWORD_LENGTH,
)

JWT_ALGORITHM = "HS256"
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")


def validate_username(username: str) -> Optional[str]:
    """Return an error message if username is invalid, else None."""
    if not _USERNAME_RE.match(username or ""):
        return "username must be 3-32 characters (letters, numbers, underscore)"
    return None


def validate_password(password: str) -> Optional[str]:
    """Return an error message if password is invalid, else None."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    return None


def hash_password(password: str) -> str:
    """Hash a plaintext password for storage."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Return True if password matches the stored bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user: dict[str, Any]) -> str:
    """Build a signed JWT for an authenticated user."""
    now = int(time.time())
    payload = {
        "sub": str(user["id"]),
        "org_id": str(user["org_id"]),
        "username": user["username"],
        "role": str(user["role"]),
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """Decode a JWT; return claims dict or None if invalid/expired."""
    if not config.JWT_SECRET:
        return None
    try:
        return jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub"]},
        )
    except jwt.PyJWTError:
        return None


def public_user(user: Any) -> dict[str, Any]:
    """Strip sensitive fields before returning a user record to clients."""
    return {
        "id": str(user.id),
        "org_id": str(user.org_id),
        "email": user.email,
        "username": user.username,
        "role": str(user.role.value if hasattr(user.role, "value") else user.role),
        "status": str(
            user.status.value if hasattr(user.status, "value") else user.status
        ),
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "approved_at": user.approved_at.isoformat() if user.approved_at else None,
    }


def extract_bearer_token(request: Request) -> Optional[str]:
    """Parse Authorization: Bearer <token> if present."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def extract_session_token(request: Request) -> Optional[str]:
    """Return JWT from Bearer header or session cookie."""
    return extract_bearer_token(request) or request.cookies.get(JWT_COOKIE_NAME)


def set_auth_cookie(response: Any, token: str) -> None:
    """Attach the HttpOnly session cookie used by the control UI."""
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        httponly=True,
        secure=config.JWT_COOKIE_SECURE,
        samesite="lax",
        max_age=JWT_EXPIRY_SECONDS,
        path="/",
    )


def clear_auth_cookie(response: Any) -> None:
    """Remove the session cookie on logout."""
    response.delete_cookie(JWT_COOKIE_NAME, path="/")
