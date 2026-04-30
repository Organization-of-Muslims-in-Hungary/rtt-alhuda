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
# OPENROUTER_TTS_RESPONSE_FORMAT=mp3
```

See [.env.example](.env.example) for all optional keys.

**Debugging OpenRouter:** On server start, the terminal prints chat/TTS URLs, models, and a **masked** `OPENROUTER_API_KEY` status. On HTTP errors (e.g. wrong key), lines prefixed with `[OpenRouter] WARN:` include status and a **truncated** response body. Set `OPENROUTER_DEBUG=1` for per-request logs (still no secrets).

## Run

```bash
python main.py
```

Open [http://127.0.0.1:3000](http://127.0.0.1:3000).

## Architecture

- The browser UI in [templates/index.html](templates/index.html) opens a WebSocket to [main.py](main.py).
- The server keeps per-session audio state, captures microphone input, and processes a rolling chunk every few seconds with a small overlap for context.
- Each chunk is sent to OpenRouter with the prior transcription and translation tail, then the returned text is appended back into the live UI.
- **TTS:** After each non-empty **English translation** segment, the server calls OpenRouter TTS (`openai/gpt-4o-mini-tts-2025-12-15` by default) and streams MP3 audio to the browser over the WebSocket.
- **Audio streaming:** The browser can subscribe to two independent audio streams over the same WebSocket connection using binary frames:
  - **Server microphone** — raw PCM (16-bit, 16 kHz, mono), prefix `0x01`
  - **TTS output** — MP3 segments, prefix `0x02`
  - Toggle via JSON messages: `{"type": "subscribe", "stream": "mic"}` / `{"type": "unsubscribe", "stream": "tts"}`

Send optional `ttsLanguage` (`"en"` or `"hu"`) on the WebSocket `start` message to pick the TTS voice mapping (`OPENROUTER_TTS_VOICE_EN` / `OPENROUTER_TTS_VOICE_HU`).

## Files

- [main.py](main.py) — process entry
- [rtt_alhuda/](rtt_alhuda/) — application package (`web_app`, `audio_*`, `tts_openrouter`, …)
- [templates/index.html](templates/index.html)
- [requirements.txt](requirements.txt)
