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
# Optional TTS (English/Hungarian use different OpenRouter `voice` IDs):
# OPENROUTER_TTS_VOICE_EN=alloy
# OPENROUTER_TTS_VOICE_HU=alloy
# OPENROUTER_TTS_RESPONSE_FORMAT=wav
# WEBRTC_STUN_URLS=stun:stun.l.google.com:19302
```

See [.env.example](.env.example) for all optional keys.

## Run

```bash
python main.py
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000).

**WebRTC test page:** [http://127.0.0.1:3000/webrtc-test.html](http://127.0.0.1:3000/webrtc-test.html) — connects the WebSocket, starts recording, then lets you negotiate `/webrtc/input` and `/webrtc/tts` and hear both tracks in-page.

## Architecture

- The browser UI in [templates/index.html](templates/index.html) opens a WebSocket to [main.py](main.py).
- The server keeps per-session audio state, captures microphone input, and processes a rolling chunk every few seconds with a small overlap for context.
- Each chunk is sent to OpenRouter with the prior transcription and translation tail, then the returned text is appended back into the live UI.
- **TTS:** After each non-empty **English translation** segment, the server calls OpenRouter [TTS](https://openrouter.ai/docs/api/api-reference/tts/create-tts) (`openai/gpt-4o-mini-tts-2025-12-15` by default) and enqueues WAV bytes for the WebRTC TTS track.
- **WebRTC (v1, single browser session):** After the UI sends WebSocket `start` (recording on), the browser can negotiate two separate peer connections:
  - `POST http://127.0.0.1:3000/webrtc/input` — JSON `{"sdp":"...","type":"offer"}` → JSON answer; **one sendonly audio track** = live **server microphone** (same stream as capture).
  - `POST http://127.0.0.1:3000/webrtc/tts` — same shape; **one sendonly audio track** = **TTS** of the latest translated phrases.

Send optional `ttsLanguage` (`"en"` or `"hu"`) on the WebSocket `start` message to pick the TTS voice mapping (`OPENROUTER_TTS_VOICE_EN` / `OPENROUTER_TTS_VOICE_HU`).

**Manual check:** Start recording from the app, then from devtools or a small page create `RTCPeerConnection`, `addTransceiver('audio', {direction:'recvonly'})`, set remote description with the POST answer, gather ICE, and verify audio on the inbound track.

## Files

- [main.py](main.py) — process entry
- [rtt_alhuda/](rtt_alhuda/) — application package (`web_app`, `audio_*`, `tts_openrouter`, `webrtc_*`, …)
- [templates/index.html](templates/index.html)
- [requirements.txt](requirements.txt)
