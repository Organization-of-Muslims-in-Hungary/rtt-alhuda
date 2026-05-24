# RTT-Alhuda

Live transcription and translation over a browser UI.

## Setup

```bash
python -m venv venv
# macOS/Linux:
source venv/bin/activate
# Windows:
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` file with:

```env
OPENROUTER_API_KEY=your_key_here
OPENROUTER_MODEL=google/gemini-3.1-flash-lite-preview
# Optional TTS (per-language OpenRouter `voice` IDs):
# OPENROUTER_TTS_VOICE_EN=alloy
# OPENROUTER_TTS_VOICE_HU=alloy
# OPENROUTER_TTS_RESPONSE_FORMAT=mp3
```

See [.env.example](.env.example) for all optional keys.

**Debugging OpenRouter:** On server start, the terminal prints chat/TTS URLs, models, and a **masked** `OPENROUTER_API_KEY` status. On HTTP errors (e.g. wrong key), lines prefixed with `[OpenRouter] WARN:` include status and a **truncated** response body. Set `OPENROUTER_DEBUG=1` for per-request logs (still no secrets).

## Run

```bash
python main.py
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000) when the server binds to localhost (default).

### Raspberry Pi (phones on the same Wi‑Fi)

1. **Bind aiohttp to all interfaces** — the default `127.0.0.1` only accepts connections from the Pi itself. For hall phones, set in `.env`:

   ```env
   RTT_ALHUDA_LISTEN_HOST=0.0.0.0
   RTT_ALHUDA_LISTEN_PORT=3000
   ```

2. **`GET /api/lan-ipv4`** — On Linux (including Raspberry Pi OS), detection uses `hostname -I` plus a UDP route guess, and prefers private IPv4 (`192.168.x.x`, `10.x`, `172.16–31.x`) so the React app’s QR can show `http://<pi-ip>:<port>/` even when the operator opened the UI as `localhost`.

3. **LAN only:** `0.0.0.0` exposes port 3000 on your local network by design. Do not port-forward it to the public internet without TLS and proper access control.

### One-shot setup script (on the Pi)

From `rtt-alhuda/` (with `../Khutba-app-frontend` present):

```bash
sudo ./scripts/pi-setup.sh --with-apt
# If Node from apt is below 20 and npm run build fails:
sudo ./scripts/pi-setup.sh --with-apt --with-node-repo

# Optional: systemd unit + start now
sudo ./scripts/pi-setup.sh --with-systemd --start-service
```

Then point nginx at `scripts/nginx-khutba-frontend.example.conf` (edit `root` and ports) so phones open one URL (React + QR + `/stream` same origin). See `scripts/pi-setup.sh` header for flags.

## Architecture

- The browser UI in [templates/index.html](templates/index.html) opens a WebSocket to [main.py](main.py).
- The server keeps per-session audio state, captures microphone input, and processes a rolling chunk every few seconds with a small overlap for context.
- Each chunk is sent to OpenRouter with the prior transcription and translation tail, then the returned text is appended back into the live UI.
- **TTS:** After each chunk, OpenRouter TTS runs **only for languages that have listeners**: legacy `{"type":"subscribe","stream":"tts"}` on `/stream`, and/or satellite sockets `GET /stream/tts/en` and `GET /stream/tts/hu` (prefixed MP3 with `0x02`). **`GET /stream/tts/ar`** streams the **original captured microphone** (same 16 kHz mono PCM as `/stream` mic, prefix `0x01`) — not Arabic text-to-speech.
- **Audio streaming:** Binary frames on `/stream`:
  - **Server microphone** — raw PCM (16-bit, 16 kHz, mono), prefix `0x01`
  - **TTS output** (legacy same socket) — MP3, prefix `0x02`, only if subscribed to `tts`
- **`GET /api/lan-ipv4`** — JSON `{"ipv4": "<addr>" | null}` for same-LAN share links (React QR in dev when the page is `localhost`).

Send optional `ttsLanguage` (`"en"` or `"hu"`) on the WebSocket `start` message for **legacy** primary-socket TTS text selection when using `subscribe` + `tts`.

## Files

- [main.py](main.py) — process entry
- [fly.toml](fly.toml) — Fly.io (2 shared CPUs, 512 MB, port 8080)
- [Dockerfile](Dockerfile) — container image for Fly / Cloud Run / Railway
- [rtt_alhuda/](rtt_alhuda/) — application package (`web_app`, `audio_*`, `tts_openrouter`, …)
- [templates/index.html](templates/index.html)
- [requirements.txt](requirements.txt)
- [docs/deploy-cloudflare.md](../docs/deploy-cloudflare.md) — Pages + Tunnel
- [docs/deploy-backend-free-hosts.md](../docs/deploy-backend-free-hosts.md) — free hosts + mic caveat
- [docs/deploy-fly-io.md](../docs/deploy-fly-io.md) — Fly deploy steps
