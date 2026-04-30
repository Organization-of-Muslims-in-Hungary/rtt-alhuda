"""Environment and audio/OpenRouter constants."""

from pathlib import Path
import os

from dotenv import load_dotenv

# Repository root (parent of the `rtt_alhuda` package directory).
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)

# Set to 1/true/yes for extra per-request OpenRouter logs (URLs, status, body snippets).
OPENROUTER_DEBUG = os.getenv("OPENROUTER_DEBUG", "").lower() in ("1", "true", "yes")

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3.1-flash-lite-preview")
PROCESSING_INTERVAL_SECONDS = 3
CONTEXT_CHUNK_COUNT = 2  # Number of past chunks to include in the payload for context
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
FRAME_CHUNK_SECONDS = 0.1
MAX_BUFFER_SECONDS = 120  # Increased to prevent dropping audio during API delays

OPENROUTER_TTS_URL = os.getenv(
    "OPENROUTER_TTS_URL", "https://openrouter.ai/api/v1/tts"
)
OPENROUTER_TTS_MODEL = os.getenv(
    "OPENROUTER_TTS_MODEL", "openai/gpt-4o-mini-tts-2025-12-15"
)
OPENROUTER_TTS_VOICE_EN = os.getenv("OPENROUTER_TTS_VOICE_EN", "alloy")
OPENROUTER_TTS_VOICE_HU = os.getenv("OPENROUTER_TTS_VOICE_HU", "alloy")
OPENROUTER_TTS_RESPONSE_FORMAT = os.getenv("OPENROUTER_TTS_RESPONSE_FORMAT", "mp3")


def voice_for_tts_language(language: str) -> str:
    """Map UI language code to OpenRouter TTS `voice` (API has no separate language field)."""

    if language == "hu":
        return OPENROUTER_TTS_VOICE_HU
    return OPENROUTER_TTS_VOICE_EN
