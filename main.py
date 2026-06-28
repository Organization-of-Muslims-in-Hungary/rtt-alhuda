"""Entry point: live transcription backend (FastAPI + uvicorn)."""

from __future__ import annotations

import os
import platform
import sys

# Work around a Windows + Python 3.14 issue where platform.system()
# can block inside a WMI query and make imports appear stuck.
if os.name == "nt" and sys.version_info >= (3, 14):
    platform.system = lambda: "Windows"

import uvicorn

from rtt_alhuda.app import create_app, log

app = create_app()

if __name__ == "__main__":
    listen_host = os.getenv("RTT_ALHUDA_LISTEN_HOST", "127.0.0.1").strip() or "127.0.0.1"
    listen_port = int(os.getenv("RTT_ALHUDA_LISTEN_PORT", "3000"))

    log(
        f"Binding uvicorn to {listen_host!r}:{listen_port} "
        f"(set RTT_ALHUDA_LISTEN_HOST=0.0.0.0 for LAN access)",
    )

    uvicorn.run(app, host=listen_host, port=listen_port)
