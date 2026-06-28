"""Admin endpoints: organization and user management."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda.database import get_db
from rtt_alhuda.db_models import Organization, Role, User, UserStatus
from rtt_alhuda.dependencies import get_org_by_slug, require_org_admin, require_superadmin
from rtt_alhuda.schemas import OrgCreate, UserCreate, UserStatusUpdate
from rtt_alhuda.security import hash_password, public_user

router = APIRouter(prefix="/api/admin")


@router.post("/orgs", response_model=None)
async def create_org(
    body: OrgCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_superadmin),
) -> dict:
    """Create a new organization (superadmin only)."""
    existing = await db.execute(
        select(Organization).where(Organization.slug == body.slug)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug_taken")
    org = Organization(name=body.name, slug=body.slug, timezone=body.timezone)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return {"ok": True, "org": {"id": str(org.id), "name": org.name, "slug": org.slug}}


@router.get("/orgs")
async def list_orgs(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_superadmin),
) -> dict:
    """List all organizations (superadmin only)."""
    result = await db.execute(select(Organization).order_by(Organization.created_at))
    orgs = result.scalars().all()
    return {
        "ok": True,
        "orgs": [
            {"id": str(o.id), "name": o.name, "slug": o.slug, "timezone": o.timezone}
            for o in orgs
        ],
    }


@router.post("/orgs/{org_slug}/users")
async def create_user(
    body: UserCreate,
    org: Organization = Depends(get_org_by_slug),
    admin: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a user inside an organization (admin/superadmin only)."""
    # Uniqueness check within the org.
    dup = await db.execute(
        select(User).where(
            User.org_id == org.id,
            (func.lower(User.username) == body.username.lower())
            | (func.lower(User.email) == body.email.lower()),
        )
    )
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "username_or_email_taken")

    role = Role(body.role)
    if role not in (Role.admin, Role.operator):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_role")

    user = User(
        org_id=org.id,
        email=body.email,
        username=body.username,
        password_hash=hash_password(body.password),
        role=role,
        status=UserStatus.active,
        approved_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"ok": True, "user": public_user(user)}


@router.get("/orgs/{org_slug}/users")
async def list_users(
    org: Organization = Depends(get_org_by_slug),
    _admin: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """List users in an organization (admin/superadmin only)."""
    result = await db.execute(
        select(User).where(User.org_id == org.id).order_by(User.created_at)
    )
    users = result.scalars().all()
    return {"ok": True, "users": [public_user(u) for u in users]}


@router.post("/orgs/{org_slug}/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    org: Organization = Depends(get_org_by_slug),
    admin: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve, suspend, or reset a user's status."""
    import uuid as _uuid

    target = await db.get(User, _uuid.UUID(user_id))
    if not target or target.org_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if target.role == Role.superadmin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_modify_superadmin")

    new_status = UserStatus(body.status)
    target.status = new_status
    target.approved_at = (
        datetime.now(timezone.utc) if new_status == UserStatus.active else None
    )
    await db.commit()
    await db.refresh(target)
    return {"ok": True, "user": public_user(target)}


@router.delete("/orgs/{org_slug}/users/{user_id}")
async def delete_user(
    user_id: str,
    org: Organization = Depends(get_org_by_slug),
    admin: User = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a user from an organization."""
    import uuid as _uuid

    target = await db.get(User, _uuid.UUID(user_id))
    if not target or target.org_id != org.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    if target.role == Role.superadmin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_delete_superadmin")
    if admin.role != Role.superadmin and target.role == Role.admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot_delete_admin")

    await db.delete(target)
    await db.commit()
    return {"ok": True}
