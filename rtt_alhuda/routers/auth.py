"""Authentication endpoints: login, logout, me, and a disabled register stub."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda import config
from rtt_alhuda.database import get_db
from rtt_alhuda.db_models import Organization, User, UserStatus
from rtt_alhuda.dependencies import get_current_user
from rtt_alhuda.schemas import LoginRequest, RegisterRequest
from rtt_alhuda.security import (
    clear_auth_cookie,
    create_access_token,
    public_user,
    set_auth_cookie,
    verify_password,
)

router = APIRouter()


async def _resolve_org(db: AsyncSession, org_slug: str | None) -> Organization:
    slug = org_slug or config.DEFAULT_ORG_SLUG
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    return org


@router.post("/api/auth/register")
async def register(body: RegisterRequest) -> JSONResponse:
    """Self-registration is disabled for now (kept for future payment/invite flow)."""
    if not config.REGISTRATION_ENABLED:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"ok": False, "reason": "registration_disabled"},
        )
    # When enabled later, this is where org assignment + pending creation goes.
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"ok": False, "reason": "not_implemented"},
    )


@router.post("/api/auth/login")
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """Authenticate and return a JWT + session cookie."""
    org = await _resolve_org(db, body.org_slug)
    result = await db.execute(
        select(User).where(
            User.org_id == org.id,
            func.lower(User.username) == body.username.strip().lower(),
        )
    )
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"ok": False, "reason": "invalid_credentials"},
        )

    if user.status == UserStatus.pending:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"ok": False, "reason": "pending_approval"},
        )
    if user.status == UserStatus.suspended:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"ok": False, "reason": "suspended"},
        )

    token = create_access_token(
        {
            "id": user.id,
            "org_id": user.org_id,
            "username": user.username,
            "role": user.role,
        }
    )
    user_public = public_user(user)
    user_public["org_slug"] = org.slug
    response = JSONResponse(
        content={"ok": True, "token": token, "user": user_public, "org_slug": org.slug}
    )
    set_auth_cookie(response, token)
    return response


@router.post("/api/auth/logout")
async def logout() -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse(content={"ok": True})
    clear_auth_cookie(response)
    return response


@router.get("/api/auth/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    """Return the current authenticated user."""
    return {"ok": True, "user": public_user(user)}
