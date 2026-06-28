"""Server/systemd control API (org-scoped, Pi-specific)."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends

from rtt_alhuda.dependencies import require_org_operator

router = APIRouter()

_XENV = {"DISPLAY": ":0", "XAUTHORITY": "/home/pi/.Xauthority"}


async def _run(cmd: str) -> tuple[int, str]:
    """Run a shell command asynchronously; return (returncode, combined output)."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **_XENV},
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    return proc.returncode, (out or b"").decode(errors="replace").strip()


@router.get("/api/orgs/{org_slug}/server/{action}")
async def server_control(action: str, _user=Depends(require_org_operator)) -> dict:
    """Restart or get the status of the juma systemd service."""
    if action == "restart":
        asyncio.get_event_loop().call_later(
            1.0,
            lambda: asyncio.ensure_future(_run("systemctl restart juma.service")),
        )
        return {"ok": True, "action": "restart", "note": "restarting in 1s"}

    if action == "status":
        rc, out = await _run("systemctl is-active juma.service")
        return {"ok": True, "active": rc == 0, "state": out}

    return {"ok": False, "reason": "unknown server action"}
