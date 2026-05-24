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

Open `http://127.0.0.1/` for the backend. If your React frontend is running in development, use `http://localhost:5175/`.


If your React dev server is on `http://localhost:5175`, the backend request will go to `http://localhost/webrtc/offer`.

## Architecture

- The browser UI in [templates/index.html](templates/index.html) opens a WebSocket to [main.py](main.py).
- The server keeps per-session audio state, captures microphone input, and processes a rolling chunk every few seconds with a small overlap for context.
- Each chunk is sent to OpenRouter with the prior transcription and translation tail, then the returned text is appended back into the live UI.

## Files

- [main.py](main.py)
- [templates/index.html](templates/index.html)
- [requirements.txt](requirements.txt)
