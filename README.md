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
```

## Run

```bash
python main.py
```

* **WebSocket Version:** [http://127.0.0.1:3000](http://127.0.0.1:3000)
* **WebRTC Version:** [http://127.0.0.1:5021](http://127.0.0.1:5021)

## Architecture

The system runs two independent server instances simultaneously:

### WebSocket (Port 3000)

* Uses `templates/index.html` to stream audio via a persistent WebSocket.
* Designed for low-latency point-to-point transcription.

### WebRTC & SSE (Port 5021)

* Uses `templates/test_webRTC.html` for a peer-to-peer audio handshake.
* **Auto-Detection:** Detects if the speaker is using Arabic, English, or Hungarian.
* **Text Streaming:** Pushes translations back to the UI via Server-Sent Events (SSE).
* **Format:** Generates a unified 3-language JSON payload for the UI.

## Files

* `main.py` — Dual-port backend handling both WebSocket and WebRTC/SSE logic.
* `templates/index.html` — Original WebSocket frontend.
* `templates/test_webRTC.html` — New WebRTC multilingual frontend.
* `requirements.txt`.
