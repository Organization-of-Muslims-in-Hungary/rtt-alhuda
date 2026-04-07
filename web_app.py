#!/usr/bin/env python3
"""RTT-Alhuda — Juma Khutba Real-Time Translation System (VAD Architecture).

Captures 48kHz audio, downsamples to 16kHz, and uses WebRTC VAD to detect
natural speech pauses. Sends complete, non-overlapping sentences to Gemini
to guarantee high accuracy and eliminate hallucination loops.
"""

import io, json, os, queue, threading, wave
import numpy as np
import sounddevice as sd
import webrtcvad
from flask import Flask, Response, render_template, jsonify
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
HARDWARE_SAMPLE_RATE = 48000
API_SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
GEMINI_MODEL = "gemini-2.5-flash-lite"

# VAD Configuration
VAD_AGGRESSIVENESS =   1      # 0 to 3 (2 is a great balance for filtering mosque noise)
FRAME_DURATION_MS = 20        # WebRTC strictly requires 10, 20, or 30ms frames
FRAME_BYTES = int(API_SAMPLE_RATE * (FRAME_DURATION_MS / 1000.0) * 2) # 640 bytes per frame
MAX_SILENCE_MS = 1500         # Send to Gemini after 1.5 seconds of silence
MIN_SPEECH_BYTES = API_SAMPLE_RATE * 2 # Ignore random coughs under 1 second long

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
app = Flask(__name__)
clients: list[queue.Queue] = []
clients_lock = threading.Lock()

state = {
    "arabic": "",
    "english": "",
    "running": False,
}

_genai_client = None

def get_genai_client():
    global _genai_client
    if _genai_client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise RuntimeError("Set GEMINI_API_KEY environment variable")
        _genai_client = genai.Client(api_key=api_key, http_options={"api_version": "v1"})
    return _genai_client

def pcm_to_wav_bytes(pcm_bytes: bytes, sr: int = API_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()

def get_strict_prompt():
    return (
        "You are a strict dictation machine. Transcribe and translate EXACTLY what is "
        "physically spoken in this Arabic audio clip. DO NOT ADD A SINGLE WORD.\n"
        "---\n"
        "RULES:\n"
        "1. DO NOT auto-complete sentences.\n"
        "2. DO NOT recite religious texts or speeches from memory.\n"
        "3. Only output the exact words heard.\n"
        "Return ONLY a JSON object with two keys: 'arabic' and 'english'."
    )

# ---------------------------------------------------------------------------
# VAD Controller
# ---------------------------------------------------------------------------
def transcription_loop():
    client = get_genai_client()
    vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
    audio_q: queue.Queue[np.ndarray] = queue.Queue()

    def audio_callback(indata, frames, time_info, status):
        audio_q.put(indata[:, 0].copy())

    stream = sd.InputStream(
        samplerate=HARDWARE_SAMPLE_RATE,
        channels=CHANNELS,
        dtype=DTYPE,
        blocksize=int(HARDWARE_SAMPLE_RATE * 0.1), # Read 100ms blocks
        callback=audio_callback,
    )

    state["running"] = True
    broadcast_event("status", {"running": True})
    
    pcm_16k_buffer = bytearray()
    speech_accumulator = bytearray()
    
    is_speaking = False
    silence_timer_ms = 0

    with stream:
        while state["running"]:
            try:
                # 1. Get 48kHz audio and downsample to 16kHz
                pcm_48k = audio_q.get(timeout=0.5)
                pcm_16k = pcm_48k[::3]
                pcm_16k_buffer.extend(pcm_16k.tobytes())
            except queue.Empty:
                continue

            # 2. Slice into exact 20ms frames for VAD analysis
            while len(pcm_16k_buffer) >= FRAME_BYTES:
                frame = bytes(pcm_16k_buffer[:FRAME_BYTES])
                del pcm_16k_buffer[:FRAME_BYTES]

        # 3. Mathematically check for human speech
                is_speech = vad.is_speech(frame, API_SAMPLE_RATE)

                if is_speech:
                    if not is_speaking:
                        print("🗣️ Speech detected! Listening...") # <-- NEW
                    is_speaking = True
                    silence_timer_ms = 0
                    speech_accumulator.extend(frame)
                else:
                    if is_speaking:
                        speech_accumulator.extend(frame)
                        silence_timer_ms += FRAME_DURATION_MS
                        
                        # 4. If Imam pauses for breath (1.5 seconds)
                        if silence_timer_ms >= MAX_SILENCE_MS:
                            print(f"⏸️ Paused for {MAX_SILENCE_MS}ms. Finalizing sentence...") # <-- NEW
                            is_speaking = False
                            silence_timer_ms = 0
                            
                            if len(speech_accumulator) > MIN_SPEECH_BYTES:
                                print("🚀 Sending audio to Gemini API...") # <-- NEW
                                process_sentence(client, bytes(speech_accumulator))
                            else:
                                print("❌ Audio too short (probably a cough). Discarding.") # <-- NEW
                                
                            speech_accumulator.clear()

    broadcast_event("status", {"running": False})

def process_sentence(client, audio_bytes):
    """Sends a complete, non-overlapping sentence to Gemini."""
    wav_data = pcm_to_wav_bytes(audio_bytes)
    
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                types.Part.from_bytes(data=wav_data, mime_type="audio/wav"),
                get_strict_prompt(),
            ],
            config=types.GenerateContentConfig(temperature=0.0)
        )
        
        text = resp.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            
        data = json.loads(text)
        new_ar = data.get("arabic", "").strip()
        new_en = data.get("english", "").strip()
        
        if new_ar or new_en:
            # We just safely append it! No overlapping text merger needed!
            state["arabic"] += (" " if state["arabic"] else "") + new_ar
            state["english"] += (" " if state["english"] else "") + new_en
            
            broadcast_event("transcript", {
                "arabic": state["arabic"],
                "english": state["english"],
            })
            
    except Exception as exc:
        print(f"[Gemini Error] {exc}")

# ---------------------------------------------------------------------------
# SSE & Web Routes
# ---------------------------------------------------------------------------
def broadcast_event(event: str, data: dict):
    payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    with clients_lock:
        for q in clients:
            q.put(payload)

def sse_stream():
    q: queue.Queue[str] = queue.Queue()
    with clients_lock:
        clients.append(q)
    try:
        yield f"event: transcript\ndata: {json.dumps({'arabic': state['arabic'], 'english': state['english']}, ensure_ascii=False)}\n\n"
        yield f"event: status\ndata: {json.dumps({'running': state['running']})}\n\n"
        while True:
            yield q.get()
    finally:
        with clients_lock:
            clients.remove(q)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stream")
def stream():
    return Response(sse_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/start", methods=["POST"])
def start():
    if not state["running"]:
        state["arabic"], state["english"] = "", ""
        threading.Thread(target=transcription_loop, daemon=True).start()
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop():
    state["running"] = False
    return jsonify({"status": "stopped"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, threaded=True)
