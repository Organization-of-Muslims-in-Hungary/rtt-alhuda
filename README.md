# RTT-Alhuda — Juma Khutba Real-Time Translation System

Real-time Arabic sermon transcription and English translation, streamed to congregation phones over local Wi-Fi.

**Raspberry Pi 5 → USB Mic → Gemini API → Flask SSE → Phone Browsers (with TTS)**

---

## Hardware

| Component | Used |
|---|---|
| Processor | Raspberry Pi 5 (8 GB) |
| Microphone | Trust Micro USB Microphone GTX 212 |
| Network | Wi-Fi router (Pi + phones on same network) |

## Quick Start

### 1. Get a Gemini API Key

Go to [https://aistudio.google.com/apikey](https://aistudio.google.com/apikey) and create a free key.

### 2. Install on the Raspberry Pi

```bash
# Clone / copy the project
cd ~/Juma

# Install system dependency for audio
sudo apt install -y libportaudio2

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install Python packages
pip install -r requirements.txt
```

### 3. Set the API Key

```bash
export GEMINI_API_KEY="your-key-here"
```

To make it permanent, add that line to `~/.bashrc`.

### 4. Run

```bash
python3 web_app.py
```

The server starts on port **5000**. Find the Pi's IP:

```bash
hostname -I
```

### 5. Connect Phones

On any phone connected to the same Wi-Fi, open:

```
http://<pi-ip>:5000
```

Press **▶ Start** to begin. Press **🔊 Audio** to hear the English translation spoken aloud.

---

## Optional: Serve on Port 80

Port 80 requires root. Instead of running as root, redirect traffic:

```bash
sudo iptables -t nat -A PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 5000
```

Now phones can just go to `http://<pi-ip>` without specifying a port.

## Auto-Start on Boot

Create a systemd service:

```bash
sudo tee /etc/systemd/system/rtt-alhuda.service << 'EOF'
[Unit]
Description=RTT-Alhuda Khutba Translation
After=network-online.target
Wants=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/Juma
Environment=GEMINI_API_KEY=your-key-here
ExecStart=/home/pi/Juma/.venv/bin/python3 web_app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now rtt-alhuda
```

---

## How It Works

1. **Audio capture** — `sounddevice` records from the USB mic at 16 kHz mono
2. **Overlapping chunks** — 6-second audio windows with 3-second stride prevent word cutoffs
3. **Gemini API** — each chunk is sent as WAV; Gemini returns Arabic transcription + English translation as JSON
4. **Text merging** — overlap-aware deduplication removes repeated words between consecutive chunks
5. **SSE broadcast** — Flask pushes updates to all connected phones in real time
6. **Client TTS** — the phone's native Web Speech API reads new English text aloud (delta-only to avoid repeats)

## Architecture

```
USB Mic → [sounddevice 16kHz] → ring buffer
                                    ↓ every 3s
                              6s WAV chunk → Gemini API
                                    ↓
                              JSON {arabic, english}
                                    ↓
                              TextMerger (dedup)
                                    ↓
                              Flask SSE → all phones
                                    ↓
                              Browser: display + TTS
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `GEMINI_API_KEY not set` | `export GEMINI_API_KEY="..."` |
| No audio detected | Check `arecord -l` — mic must be listed. Try `sudo apt install alsa-utils` |
| Phones can't connect | Ensure Pi and phones are on the same Wi-Fi network |
| TTS not working on iPhone | Tap the **🔊 Audio** button — iOS requires a user gesture to unlock speech |
| High latency | Normal: ~6-9 seconds. Ensure stable internet on the Pi |

## Project Structure

```
Juma/
├── web_app.py          # Backend: audio capture, Gemini API, Flask SSE
├── requirements.txt    # Python dependencies
├── templates/
│   └── index.html      # Frontend: Arabic/English display, TTS
├── details.md          # Original project specification
└── README.md           # This file
```
