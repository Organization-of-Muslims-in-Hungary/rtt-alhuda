# RTT-Alhuda: Real-Time Arabic Transcription and Translation

A real-time speech transcription and translation application that captures Arabic audio from your microphone and provides live transcription with English translation using Google's Gemini AI.

## Features

- **Live Audio Capture**: Records audio from your microphone in configurable chunks
- **Real-Time Transcription**: Transcribes Arabic speech as it's spoken
- **Live Translation**: Provides English translation alongside Arabic transcription
- **Overlap Processing**: Uses overlapping audio windows to maintain context across chunk boundaries
- **Web Interface**: Clean, modern UI showing live transcription, translation, and debug information
- **Silence Detection**: Automatically skips processing for silent audio chunks
- **Queue Management**: Handles audio processing backlog gracefully

## Requirements

- Python 3.10+
- A working microphone
- Google Gemini API key

## Installation

1. Clone or download this repository:

   ```bash
   cd rtt-alhuda
   ```

2. Create and activate a virtual environment:

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Create a `.env` file with your Gemini API key:

   ```bash
   echo "GEMINI_API_KEY=your_api_key_here" > .env
   ```

   You can obtain an API key from [Google AI Studio](https://aistudio.google.com/apikey).

## Usage

### Web Application (Recommended)

Run the Flask web server:

```bash
python3 web_app.py
```

Open your browser to [http://127.0.0.1:5021](http://127.0.0.1:5021).

Use the interface to:
- Click **Start Recording** to begin capturing audio
- Click **Stop Recording** to stop
- View live transcription (Arabic) and translation (English) as they stream in
- Monitor audio chunks, queue status, and raw model responses in the debug panels

### Command-Line Mode

For a simpler command-line experience:

```bash
python3 app.py
```

This runs continuous transcription directly in the terminal, printing results as JSON.

## Configuration

Key parameters can be adjusted in the source files or via environment variables.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | (required) | Your Google Gemini API key |
| `RTT_LOG_LEVEL` | DEBUG | Logging level (DEBUG, INFO, WARNING, ERROR) |

### `web_app.py` - TranscriptionController

| Parameter | Default | Description |
|-----------|---------|-------------|
| `chunk_duration_seconds` | 10.0 | Length of each audio chunk |
| `overlap_ratio` | 0.5 | Overlap between consecutive chunks (0-1) |
| `sample_rate` | 16000 | Audio sample rate in Hz |
| `silence_rms_threshold` | 0.0005 | RMS threshold below which audio is considered silence |
| `tail_context_words` | 15 | Number of words from previous chunk to use as context |

### `app.py` - Model Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MODEL_NAME` | gemini-2.5-flash-lite | Gemini model to use |
| `MAX_OUTPUT_TOKENS` | 260 | Maximum tokens in model response |
| `TEMPERATURE` | 0.0 | Model temperature (0 = deterministic) |

## Logging and Debugging

The application includes comprehensive logging for debugging and monitoring.

### Log Files

Logs are stored in the `logs/` directory:

- `app.log` - Core transcription logic (CLI mode)
- `web_app.log` - Web server and controller logs

Log files are automatically rotated at 10 MB with 5 backup files retained.

### Log Levels

Set the log level via the `RTT_LOG_LEVEL` environment variable:

```bash
# In .env file
RTT_LOG_LEVEL=DEBUG   # Most verbose - all debug info
RTT_LOG_LEVEL=INFO    # Standard - key events and results
RTT_LOG_LEVEL=WARNING # Minimal - only warnings and errors
```

### What Gets Logged

- **DEBUG**: Detailed timing breakdowns, JSON parsing steps, queue operations
- **INFO**: Session start/stop, chunk processing results, API initialization
- **WARNING**: Audio overflow, queue drops, JSON parse failures
- **ERROR**: API errors, microphone failures, processing exceptions

### API Endpoints for Monitoring

The web app provides additional endpoints for debugging:

| Endpoint | Description |
|----------|-------------|
| `GET /status` | Current recording state and queue size |
| `GET /stats` | Detailed session statistics (chunks processed, timing, errors) |

Example stats response:

```json
{
  "session_duration_seconds": 125.5,
  "chunks_recorded": 25,
  "chunks_processed": 23,
  "chunks_skipped_silence": 2,
  "chunks_dropped": 0,
  "total_audio_mb": 3.84,
  "avg_process_time_seconds": 1.234,
  "errors": 0,
  "queue_size": 1,
  "subscribers": 2
}
```

### Viewing Logs in Real-Time

```bash
# Watch web app logs
tail -f logs/web_app.log

# Watch CLI logs
tail -f logs/app.log

# Filter for errors only
tail -f logs/web_app.log | grep -E "ERROR|WARNING"
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Web Browser                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Transcription Panel  │  Translation Panel          │   │
│  │  (Arabic text)        │  (English text)             │   │
│  └─────────────────────────────────────────────────────┘   │
│           ↑ Server-Sent Events (SSE)                        │
└───────────│─────────────────────────────────────────────────┘
            │
┌───────────│─────────────────────────────────────────────────┐
│           ↓                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────┐ │
│  │  Recorder   │───→│ Audio Queue │───→│   Processor     │ │
│  │  Thread     │    │             │    │   Thread        │ │
│  └─────────────┘    └─────────────┘    └────────│────────┘ │
│        ↑                                        │          │
│   Microphone                                    ↓          │
│                                         Gemini API         │
│                        Flask Server (web_app.py)           │
└─────────────────────────────────────────────────────────────┘
```

### Components

- **Recorder Thread**: Continuously captures audio from the microphone in overlapping chunks
- **Audio Queue**: Buffers recorded chunks for processing
- **Processor Thread**: Sends audio to Gemini API and processes responses
- **SSE Stream**: Pushes real-time updates to connected browsers
- **Text Merger**: Handles overlapping transcriptions to produce coherent output

## File Structure

```
rtt-alhuda/
├── app.py              # Core transcription logic and CLI mode
├── web_app.py          # Flask web server and UI controller
├── templates/
│   └── index.html      # Web interface
├── logs/               # Log files (auto-created)
│   ├── app.log         # CLI mode logs
│   └── web_app.log     # Web server logs
├── requirements.txt    # Python dependencies
├── .env                # API key and settings (create this)
└── .gitignore
```

## Troubleshooting

### No audio input detected
- Ensure your microphone is connected and working
- Check that the correct input device is selected in your system settings
- On Linux, you may need to install `portaudio`: `sudo apt install libportaudio2`

### API errors
- Verify your `GEMINI_API_KEY` is correct in the `.env` file
- Check your API quota at [Google AI Studio](https://aistudio.google.com/)

### High latency
- Reduce `chunk_duration_seconds` for faster (but potentially less accurate) results
- Ensure stable internet connection
- Check the queue size in the UI; if it grows continuously, processing can't keep up

## License

This project is provided as-is for educational and personal use.
