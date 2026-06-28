"""Public health and LAN discovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter

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
