"""FastAPI dependencies: database session, auth, role enforcement, org scoping."""

from __future__ import annotations

import uuid
from typing import Optional

from aiohttp import ClientSession
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda.database import get_db
from rtt_alhuda.db_models import Organization, Role, User, UserStatus
from rtt_alhuda.models import ServerSession
from rtt_alhuda.security import decode_access_token, extract_session_token


def get_http_client(request: Request) -> ClientSession:
    """Return the shared aiohttp client used for OpenRouter calls."""
    return request.app.state.http_client


def get_session_manager(request: Request):
    """Return the per-org recording session manager."""
    return request.app.state.session_manager


async def get_optional_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Load the current user from JWT + database, or None if unauthenticated."""
    token = extract_session_token(request)
    if not token:
        return None
    claims = decode_access_token(token)
    if not claims:
        return None
    user_id = claims.get("sub")
    if not user_id:
        return None
    result = await db.get(User, uuid.UUID(user_id))
    return result


async def get_current_user(
    user: Optional[User] = Depends(get_optional_user),
) -> User:
    """Require an authenticated user."""
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unauthorized")
    return user


async def require_active_user(user: User = Depends(get_current_user)) -> User:
    """Require the user account to be active."""
    if user.status != UserStatus.active:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            user.status.value if hasattr(user.status, "value") else str(user.status),
        )
    return user


def require_role(*roles: Role):
    """Build a dependency that enforces one of the given roles."""

    async def _check(user: User = Depends(require_active_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return user

    return _check


require_superadmin = require_role(Role.superadmin)
require_admin = require_role(Role.superadmin, Role.admin)
require_operator = require_role(Role.superadmin, Role.admin, Role.operator)


async def get_org_by_slug(
    org_slug: str,
    db: AsyncSession = Depends(get_db),
) -> Organization:
    """Look up an organization by its slug."""
    result = await db.execute(
        select(Organization).where(Organization.slug == org_slug)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    return org


def require_org_access(
    org: Organization = Depends(get_org_by_slug),
    user: User = Depends(require_active_user),
) -> User:
    """Ensure the user belongs to the requested org (superadmins bypass)."""
    if user.role == Role.superadmin:
        return user
    if user.org_id != org.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return user


def require_org_admin(
    org: Organization = Depends(get_org_by_slug),
    user: User = Depends(require_active_user),
) -> User:
    """Ensure the user is an admin/superadmin for the requested org."""
    if user.role == Role.superadmin:
        return user
    if user.role != Role.admin or user.org_id != org.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return user


def require_org_operator(
    org: Organization = Depends(get_org_by_slug),
    user: User = Depends(require_active_user),
) -> User:
    """Ensure the user is an operator/admin/superadmin for the requested org."""
    if user.role == Role.superadmin:
        return user
    if user.role not in (Role.admin, Role.operator) or user.org_id != org.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    return user


async def get_org_session(
    org: Organization = Depends(get_org_by_slug),
    session_manager=Depends(get_session_manager),
) -> ServerSession:
    """Return the runtime ServerSession for the requested org."""
    return await session_manager.get_or_create(org.id)
