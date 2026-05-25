"""Entry point: live transcription backend for the browser app.

The app runs a single aiohttp server that serves `index.html` and accepts a
WebSocket connection at `/stream`. When the browser sends `start`, the server
captures microphone audio locally, keeps a rolling PCM buffer, converts audio
windows to WAV, and sends them to OpenRouter for transcription and translation.

Implementation lives in the `rtt_alhuda` package (see docs/superpowers/specs/).

(TODO: in production, make it work headlessly with no frontend)
"""

import os
import platform
import sys

# Work around a Windows + Python 3.14 issue where platform.system()
# can block inside a WMI query and make aiohttp import appear stuck.
if os.name == "nt" and sys.version_info >= (3, 14):
    platform.system = lambda: "Windows"

from aiohttp import web

from rtt_alhuda.web_app import create_app, log


def _resolve_listen_port() -> int:
    """Pick HTTP listen port for local and Fly runtimes."""

    on_fly = bool(os.getenv("FLY_APP_NAME") or os.getenv("FLY_ALLOC_ID"))
    if on_fly:
        # Fly health checks and proxy routing target internal_port (8080 in fly.toml).
        # Prefer PORT when present; ignore legacy 3000 values that can come from stale env/secrets.
        fly_port = os.getenv("PORT", "").strip()
        if fly_port.isdigit():
            return int(fly_port)
        app_port = os.getenv("RTT_ALHUDA_LISTEN_PORT", "").strip()
        if app_port.isdigit() and int(app_port) != 3000:
            return int(app_port)
        return 8080

    raw = os.getenv("RTT_ALHUDA_LISTEN_PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return 3000


if __name__ == "__main__":
    listen_host = os.getenv("RTT_ALHUDA_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    listen_port = _resolve_listen_port()

    log(
        f"Binding aiohttp to {listen_host!r}:{listen_port} "
        f"(set RTT_ALHUDA_LISTEN_HOST=0.0.0.0 on a Raspberry Pi for phones on the same Wi‑Fi)",
    )
    if listen_host in ("0.0.0.0", "::"):
        log(
            "Reachable on this machine's LAN IP, e.g. "
            f"http://<this-pi-ip>:{listen_port}/ — QR /api/lan-ipv4 uses the same hint logic",
        )

    web.run_app(create_app(), host=listen_host, port=listen_port)
