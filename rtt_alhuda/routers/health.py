"""Public health and LAN discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda.database import get_db
from rtt_alhuda.db_models import Organization
from rtt_alhuda.lan_detect import detect_lan_ipv4

router = APIRouter()


@router.get("/api/health")
async def health() -> dict:
    """Liveness probe for compose/systemd."""
    return {"ok": True, "status": "healthy"}


@router.get("/api/lan-ipv4")
async def lan_ipv4() -> dict:
    """JSON for dev QR codes: ``{"ipv4": "<addr>" | null}``."""
    return {"ipv4": detect_lan_ipv4()}


@router.get("/api/orgs/{org_slug}")
async def org_lookup(org_slug: str, db: AsyncSession = Depends(get_db)) -> dict:
    """Public org existence check. Returns org name/slug or 404."""
    result = await db.execute(
        select(Organization).where(Organization.slug == org_slug)
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "organization not found")
    return {"ok": True, "org": {"id": str(org.id), "name": org.name, "slug": org.slug}}
