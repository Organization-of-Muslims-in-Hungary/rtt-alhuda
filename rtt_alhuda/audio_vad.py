"""Voice activity detection helpers."""

import webrtcvad

from rtt_alhuda.config import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH_BYTES

vad = webrtcvad.Vad(3)  # aggressiveness 0-3 (2 is aggressive)


def is_speech_present(pcm_data: bytes) -> bool:
    """Check if a PCM audio chunk contains speech using VAD."""
    try:
        # VAD requires exact frame durations: 10, 20, or 30 ms.
        frame_duration_ms = 30
        frame_bytes = int(
            (SAMPLE_RATE * frame_duration_ms / 1000.0) * CHANNELS * SAMPLE_WIDTH_BYTES
        )

        consecutive_speech_frames = 0
        min_consecutive_frames = 15  # 450 ms of continuous speech at 30 ms per frame

        for i in range(0, len(pcm_data) - frame_bytes + 1, frame_bytes):
            frame = pcm_data[i : i + frame_bytes]
            if vad.is_speech(frame, SAMPLE_RATE):
                consecutive_speech_frames += 1
                if consecutive_speech_frames >= min_consecutive_frames:
                    return True
            else:
                consecutive_speech_frames = 0

        return False
    except Exception as e:
        print(f"VAD error: {e}")
        return True  # Fallback to true so we don't drop audio falsely
