# Juma Khutba Real-Time Translation System (RTT-Alhuda)

## 📖 Project Overview
The RTT-Alhuda project aims to bridge the language barrier during Friday (Juma) sermons by providing a real-time, AI-powered transcription and translation system. It captures the Imam's Arabic speech, processes it via cloud-based AI, and broadcasts the English translation and Arabic transcription to the congregation's smartphones over a local network connection. It also features client-side Text-to-Speech (TTS) for accessibility.

## 🛠️ Hardware & Tools Used

### Hardware
* **Core Processor:** Raspberry Pi 5 (8GB RAM) running Raspberry Pi OS (64-bit).
* **Audio Input:** Trust Micro USB Microphone GTX 212.
* **Network:** Wi-Fi router/hotspot connecting the Raspberry Pi to the internet (for API access) and the local congregation.

### Software & Libraries
* **Language:** Python 3.13+
* **Audio Capture:** `sounddevice` (Python library for continuous audio streaming).
* **AI Engine:** Google Gemini API (`gemini-2.5-flash-lite`). Chosen for ultra-low latency and cost-effectiveness.
* **Web Server:** `Flask` (Lightweight Python web framework serving the frontend on port `0.0.0.0:80`).
* **Real-time Communication:** Server-Sent Events (SSE) to push continuous text updates to clients without page reloads.
* **Frontend:** HTML5, CSS3, JavaScript, and the Web Speech API for native Text-to-Speech generation.

---

## ⚙️ How It Works (The Architecture)

The system is designed to run continuously during a 30-minute sermon without dropping context. It utilizes an "overlapping chunk" architecture to handle continuous speech.

1.  **Audio Capture:** The Python backend (`web_app.py`) continuously listens to the USB microphone at a sample rate of `48000 Hz`.
2.  **Slicing & Strides:** The audio is sliced into overlapping chunks (e.g., 8-second duration with a 4-second stride/overlap). This prevents the AI from cutting off words mid-sentence at the boundary of a recording.
3.  **AI Processing:** The backend packages the audio chunk and sends it to the Gemini API with a specific prompt to extract "stable" (finalized) and "unstable" (provisional/tail) translations.
4.  **Merging:** The `TranscriptionController` merges the overlapping chunks, resolving any duplicated words (overlap hallucinations) before committing the final sentence.
5.  **Broadcasting:** The processed text is pushed via Flask's `/stream` endpoint to any connected mobile devices.
6.  **Client-Side Rendering:** The user's phone receives the SSE payload, instantly updates the HTML text elements, and triggers the phone's native OS voice to read the English translation aloud if the user enabled audio.

---

## 🔄 Project Flow Chart

```mermaid
graph TD
    subgraph Input Layer
        A[Imam Speaks Arabic] -->|Physical Sound| B(Trust USB Microphone)
    end

    subgraph Processing Layer - Raspberry Pi 5
        B -->|48kHz Audio Stream| C{Python: sounddevice}
        C -->|Slices 8s Audio Chunks| D[Audio Queue]
        D -->|Pulls Chunk| E[Transcription Controller]
    end

    subgraph Cloud AI Layer
        E -->|Sends Audio + Prompt via Internet| F((Google Gemini API))
        F -->|Returns JSON Transcript/Translation| E
    end

    subgraph Output Layer - Web Server
        E -->|Cleans & Merges Text| G[Flask Web Server port: 80]
        G -->|Server-Sent Events SSE| H((Local Wi-Fi Network))
    end

    subgraph Client Layer - Congregation Phones
        H --> I[Mobile Browser UI]
        I -->|Displays| J[Arabic Transcription]
        I -->|Displays| K[English Translation]
        I -->|Triggers Web Speech API| L[Phone Speaks English Audio]
    end
