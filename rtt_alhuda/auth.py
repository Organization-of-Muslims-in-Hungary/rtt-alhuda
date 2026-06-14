"""Password hashing, JWT sessions, and auth helpers for operator accounts."""

from __future__ import annotations

import re
import time
from typing import Any, Optional

import bcrypt
import jwt
from aiohttp import web

from rtt_alhuda.config import (
    JWT_COOKIE_NAME,
    JWT_EXPIRY_SECONDS,
    JWT_SECRET,
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
    """Build a signed JWT for an approved operator or admin."""
    now = int(time.time())
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "role": user["role"],
        "iat": now,
        "exp": now + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """Decode a JWT; return claims dict or None if invalid/expired."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Strip sensitive fields before returning a user record to clients."""
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "status": user["status"],
        "created_at": user["created_at"],
        "approved_at": user.get("approved_at"),
        "approved_by": user.get("approved_by"),
    }


def extract_bearer_token(request: web.Request) -> Optional[str]:
    """Parse Authorization: Bearer <token> if present."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def extract_session_token(request: web.Request) -> Optional[str]:
    """Return JWT from Bearer header or session cookie."""
    return extract_bearer_token(request) or request.cookies.get(JWT_COOKIE_NAME)


def set_auth_cookie(response: web.Response, token: str) -> None:
    """Attach the HttpOnly session cookie used by the control HTML UI."""
    response.set_cookie(
        JWT_COOKIE_NAME,
        token,
        httponly=True,
        samesite="Lax",
        max_age=JWT_EXPIRY_SECONDS,
        path="/",
    )


def clear_auth_cookie(response: web.Response) -> None:
    """Remove the session cookie on logout."""
    response.del_cookie(JWT_COOKIE_NAME, path="/")


async def resolve_user(request: web.Request) -> Optional[dict[str, Any]]:
    """Load the current user from JWT + database, or None if unauthenticated."""
    token = extract_session_token(request)
    if not token:
        return None

    claims = decode_access_token(token)
    if not claims:
        return None

    from rtt_alhuda import db as client_db

    db = request.app["client_db"]
    user = await client_db.get_user_by_id(db, claims["sub"])
    if not user:
        return None
    return user


def can_access_control(user: dict[str, Any]) -> bool:
    """True if user may use operator control APIs and pages."""
    return user["status"] == "approved"


def is_admin(user: dict[str, Any]) -> bool:
    return user.get("role") == "admin"


def path_requires_auth(path: str, method: str) -> bool:
    """Return True if the route requires an approved operator session."""
    if path.startswith("/api/admin/"):
        return True
    if path.startswith("/api/control/"):
        return True
    if path.startswith("/api/browser/"):
        return True
    if path.startswith("/api/server/"):
        return True
    if path == "/api/clients" and method == "GET":
        return True
    if path.startswith("/api/clients/") and path.endswith("/rename") and method == "POST":
        return True
    if path.startswith("/api/clients/") and method == "DELETE":
        return True
    return False


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Enforce JWT auth on control and admin API routes."""
    path = request.path
    method = request.method

    if not path_requires_auth(path, method):
        return await handler(request)

    user = await resolve_user(request)
    if not user:
        return web.json_response({"ok": False, "reason": "unauthorized"}, status=401)

    if path.startswith("/api/admin/"):
        if not is_admin(user):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
    elif not can_access_control(user):
        reason = user["status"] if user["status"] in ("pending", "rejected") else "forbidden"
        return web.json_response({"ok": False, "reason": reason}, status=403)

    request["user"] = user
    return await handler(request)
