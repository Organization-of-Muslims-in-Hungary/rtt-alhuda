"""PCM to WAV container encoding."""

import wave
from io import BytesIO

from rtt_alhuda.config import SAMPLE_WIDTH_BYTES


def create_wav_bytes(pcm_data: bytes, sample_rate: int, channels: int) -> bytes:
    """Wrap raw PCM bytes in a WAV container for OpenRouter audio input."""

    wav_buffer = BytesIO()
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return wav_buffer.getvalue()
