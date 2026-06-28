"""Device registry API (org-scoped)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda.database import get_db
from rtt_alhuda.dependencies import get_org_by_slug, get_org_session, require_org_operator
from rtt_alhuda.db_models import Organization
from rtt_alhuda.device_service import (
    delete_device,
    list_devices,
    register_device,
    rename_device,
)
from rtt_alhuda.models import ServerSession
from rtt_alhuda.schemas import DeviceRegister, DeviceRename

router = APIRouter()


@router.post("/api/orgs/{org_slug}/devices")
async def devices_register(
    body: DeviceRegister,
    request: Request,
    org: Organization = Depends(get_org_by_slug),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register or re-identify a display device (public, no auth)."""
    ua = request.headers.get("User-Agent", "")
    device = await register_device(
        db,
        org.id,
        body.client_id,
        body.name,
        body.screen_w,
        body.screen_h,
        ua,
    )
    return {"ok": True, "client": device}


@router.get("/api/orgs/{org_slug}/devices")
async def devices_list(
    org: Organization = Depends(get_org_by_slug),
    _user=Depends(require_org_operator),
    db: AsyncSession = Depends(get_db),
    session: ServerSession = Depends(get_org_session),
) -> dict:
    """List all known devices with online status (operator/admin only)."""
    devices = await list_devices(db, org.id)
    connected_ids = set(session.client_sse_map.keys())
    for d in devices:
        d["connected"] = d["id"] in connected_ids
    return {"ok": True, "clients": devices}


@router.post("/api/orgs/{org_slug}/devices/{device_id}/rename")
async def devices_rename(
    body: DeviceRename,
    device_id: str,
    org: Organization = Depends(get_org_by_slug),
    _user=Depends(require_org_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Rename a device (operator/admin only)."""
    ok = await rename_device(db, org.id, device_id, body.name)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return {"ok": True}


@router.delete("/api/orgs/{org_slug}/devices/{device_id}")
async def devices_delete(
    device_id: str,
    org: Organization = Depends(get_org_by_slug),
    _user=Depends(require_org_operator),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove a device from the registry (operator/admin only)."""
    ok = await delete_device(db, org.id, device_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    return {"ok": True}
