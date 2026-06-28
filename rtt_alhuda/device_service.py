"""Device registry helpers shared by the devices API and SSE auto-registration."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rtt_alhuda.db_models import Device, DeviceType


def _device_type(screen_w: int, screen_h: int) -> DeviceType:
    if screen_w and screen_h:
        return DeviceType.phone if max(screen_w, screen_h) < 1024 else DeviceType.screen
    return DeviceType.unknown


def device_to_dict(device: Device, connected: bool = False) -> dict:
    """Serialize a Device ORM object to a JSON-friendly dict."""
    return {
        "id": str(device.id),
        "org_id": str(device.org_id),
        "name": device.name,
        "device_type": device.device_type.value,
        "screen_w": device.screen_w,
        "screen_h": device.screen_h,
        "user_agent": device.user_agent,
        "first_seen": device.first_seen.isoformat() if device.first_seen else None,
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "connected": connected,
    }


async def register_device(
    db: AsyncSession,
    org_id: uuid.UUID,
    client_id: Optional[str],
    name: str,
    screen_w: int,
    screen_h: int,
    user_agent: str,
) -> dict:
    """Register a new device or update an existing one. Returns the device dict."""
    now = datetime.now(timezone.utc)
    dtype = _device_type(screen_w, screen_h)

    if client_id:
        try:
            device = await db.get(Device, uuid.UUID(client_id))
        except (ValueError, TypeError):
            device = None
        if device and device.org_id == org_id:
            # Only overwrite name if the caller actually provided one.
            if name:
                device.name = name
            device.device_type = dtype
            device.screen_w = screen_w
            device.screen_h = screen_h
            device.last_seen = now
            device.user_agent = user_agent
            await db.commit()
            await db.refresh(device)
            return device_to_dict(device)

    device = Device(
        org_id=org_id,
        name=name,
        device_type=dtype,
        screen_w=screen_w,
        screen_h=screen_h,
        user_agent=user_agent,
        first_seen=now,
        last_seen=now,
    )
    db.add(device)
    await db.commit()
    await db.refresh(device)
    return device_to_dict(device)


async def touch_device(db: AsyncSession, device_id: str) -> None:
    """Update last_seen timestamp for a device."""
    try:
        device = await db.get(Device, uuid.UUID(device_id))
    except (ValueError, TypeError):
        return
    if device:
        device.last_seen = datetime.now(timezone.utc)
        await db.commit()


async def rename_device(
    db: AsyncSession, org_id: uuid.UUID, device_id: str, name: str
) -> bool:
    """Rename a device. Returns True if the device existed in the org."""
    try:
        device = await db.get(Device, uuid.UUID(device_id))
    except (ValueError, TypeError):
        return False
    if not device or device.org_id != org_id:
        return False
    device.name = name
    await db.commit()
    return True


async def list_devices(db: AsyncSession, org_id: uuid.UUID) -> list[dict]:
    """Return all devices for an org ordered by last_seen desc."""
    result = await db.execute(
        select(Device)
        .where(Device.org_id == org_id)
        .order_by(Device.last_seen.desc())
    )
    return [device_to_dict(d) for d in result.scalars().all()]


async def delete_device(db: AsyncSession, org_id: uuid.UUID, device_id: str) -> bool:
    """Remove a device from an org. Returns True if deleted."""
    try:
        device = await db.get(Device, uuid.UUID(device_id))
    except (ValueError, TypeError):
        return False
    if not device or device.org_id != org_id:
        return False
    await db.delete(device)
    await db.commit()
    return True
