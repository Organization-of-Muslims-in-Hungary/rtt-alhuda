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

# Operator authentication (control page / admin APIs).
JWT_COOKIE_NAME = "khutba_token"
JWT_EXPIRY_SECONDS = 7 * 24 * 3600
MIN_PASSWORD_LENGTH = 8
JWT_SECRET: str | None = None
DEFAULT_ADMIN_USERNAME: str | None = None
DEFAULT_ADMIN_PASSWORD: str | None = None
JWT_COOKIE_SECURE = os.getenv("KHUTBA_COOKIE_SECURE", "").lower() in ("1", "true", "yes")


def validate_auth_config() -> None:
    """Load required auth settings from the environment or fail closed."""
    global JWT_SECRET, DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD

    JWT_SECRET = os.getenv("KHUTBA_JWT_SECRET")
    DEFAULT_ADMIN_USERNAME = os.getenv("KHUTBA_ADMIN_USERNAME")
    DEFAULT_ADMIN_PASSWORD = os.getenv("KHUTBA_ADMIN_PASSWORD")

    missing = [
        name
        for name, value in (
            ("KHUTBA_JWT_SECRET", JWT_SECRET),
            ("KHUTBA_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME),
            ("KHUTBA_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required auth environment variables: " + ", ".join(missing)
        )


def voice_for_tts_language(language: str) -> str:
    """Map UI language code to OpenRouter TTS `voice` (API has no separate language field)."""

    if language == "hu":
        return OPENROUTER_TTS_VOICE_HU
    return OPENROUTER_TTS_VOICE_EN
