"""Tests for WebRTC audio helpers."""

import wave
from io import BytesIO

from rtt_alhuda.webrtc_tracks import _wav_bytes_to_48k_frames


def _minimal_wav_pcm16_mono() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x01" * 320)
    return buf.getvalue()


def test_wav_bytes_to_48k_frames_decodes() -> None:
    wav = _minimal_wav_pcm16_mono()
    frames = _wav_bytes_to_48k_frames(wav)
    assert len(frames) >= 1
    assert frames[0].sample_rate == 48000
    assert frames[0].format.name == "s16"


def test_wav_bytes_invalid_returns_empty() -> None:
    assert _wav_bytes_to_48k_frames(b"not a wav") == []
