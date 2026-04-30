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

if __name__ == "__main__":
    log("Server is running on http://localhost:3000")
    web.run_app(create_app(), host="127.0.0.1", port=3000)
