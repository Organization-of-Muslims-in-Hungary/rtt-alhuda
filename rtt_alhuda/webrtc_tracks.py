"""aiortc outbound audio tracks: server mic (16 kHz PCM) and TTS (WAV bytes)."""

from __future__ import annotations

import asyncio
import fractions
import time
from collections import deque
from io import BytesIO
import av
from aiortc.mediastreams import AUDIO_PTIME, AudioStreamTrack, MediaStreamError
from av import AudioFrame

from rtt_alhuda.config import SAMPLE_RATE

# 20 ms of mono s16 at capture rate (used for fixed input slice to resampler).
SAMPLES_20MS_16K = int(SAMPLE_RATE * AUDIO_PTIME)
BYTES_20MS_16K = SAMPLES_20MS_16K * 2

# Opus/WebRTC-friendly packet at 48 kHz.
SAMPLES_20MS_48K = int(48000 * AUDIO_PTIME)


def _silent_frame_48k() -> AudioFrame:
    frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_20MS_48K)
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    frame.sample_rate = 48000
    return frame


def _normalize_48k_s16_mono(frame: av.AudioFrame, target_samples: int = SAMPLES_20MS_48K) -> AudioFrame:
    """Trim or pad resampler output to a fixed 20 ms @ 48 kHz frame."""

    raw = bytes(frame.planes[0])
    need = target_samples * 2
    if len(raw) < need:
        raw = raw + b"\x00" * (need - len(raw))
    elif len(raw) > need:
        raw = raw[:need]
    out = AudioFrame(format="s16", layout="mono", samples=target_samples)
    out.planes[0].update(raw)
    out.sample_rate = 48000
    return out


def _pcm_16k_to_48k(pcm_16k: bytes) -> AudioFrame:
    """Resample one 20 ms mono s16 chunk from SAMPLE_RATE to 48 kHz."""

    samples = len(pcm_16k) // 2
    if samples == 0:
        return _silent_frame_48k()
    f_in = AudioFrame(format="s16", layout="mono", samples=samples)
    f_in.planes[0].update(pcm_16k)
    f_in.sample_rate = SAMPLE_RATE
    resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
    parts = list(resampler.resample(f_in))
    if not parts:
        return _silent_frame_48k()
    return _normalize_48k_s16_mono(parts[0])


def _audio_bytes_to_48k_frames(audio_bytes: bytes) -> list[AudioFrame]:
    """Decode audio bytes (MP3, WAV, PCM container) into fixed-size 48 kHz mono s16 frames."""

    out_frames: list[AudioFrame] = []
    if not audio_bytes:
        return out_frames
    container = None
    try:
        container = av.open(BytesIO(audio_bytes), mode="r")
        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            return out_frames
        audio_stream = audio_streams[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        for packet in container.demux(audio_stream):
            for decoded in packet.decode():
                if getattr(decoded, "sample_rate", None) in (None, 0):
                    decoded.sample_rate = SAMPLE_RATE
                for rf in resampler.resample(decoded):
                    out_frames.append(_normalize_48k_s16_mono(rf))
    except (OSError, ValueError, StopIteration):
        return out_frames
    finally:
        if container is not None:
            container.close()
    return out_frames


class MicPcmTrack(AudioStreamTrack):
    """Live server microphone: int16 mono PCM chunks at SAMPLE_RATE → 48 kHz WebRTC."""

    kind = "audio"

    def __init__(self, pcm_queue: asyncio.Queue[bytes]) -> None:
        super().__init__()
        self._queue = pcm_queue
        self._buffer = bytearray()
        self._timestamp = 0
        self._start: float | None = None

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        sample_rate = 48000
        if self._start is None:
            self._start = time.time()
            self._timestamp = 0

        self._timestamp += SAMPLES_20MS_48K
        assert self._start is not None
        wait = self._start + (self._timestamp / sample_rate) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

        # Fill up to one 20 ms slice at capture rate.
        while len(self._buffer) < BYTES_20MS_16K:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                self._buffer.extend(chunk)
            except asyncio.TimeoutError:
                break

        if len(self._buffer) >= BYTES_20MS_16K:
            slice_bytes = bytes(self._buffer[:BYTES_20MS_16K])
            del self._buffer[:BYTES_20MS_16K]
        else:
            slice_bytes = bytes(self._buffer).ljust(BYTES_20MS_16K, b"\x00")
            self._buffer.clear()

        frame = _pcm_16k_to_48k(slice_bytes)
        frame.pts = self._timestamp
        frame.sample_rate = sample_rate
        frame.time_base = fractions.Fraction(1, sample_rate)
        return frame


class TtsAudioTrack(AudioStreamTrack):
    """Plays TTS responses (MP3/PCM from OpenRouter) as a timed 48 kHz stream."""

    kind = "audio"

    def __init__(self, audio_queue: asyncio.Queue[bytes]) -> None:
        super().__init__()
        self._audio_queue = audio_queue
        self._pending: deque[AudioFrame] = deque()
        self._timestamp = 0
        self._start: float | None = None

    async def recv(self) -> AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        sample_rate = 48000
        if self._start is None:
            self._start = time.time()
            self._timestamp = 0

        self._timestamp += SAMPLES_20MS_48K
        assert self._start is not None
        wait = self._start + (self._timestamp / sample_rate) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

        while not self._pending:
            try:
                audio_blob = await asyncio.wait_for(self._audio_queue.get(), timeout=0.05)
            except asyncio.TimeoutError:
                break
            for fr in _audio_bytes_to_48k_frames(audio_blob):
                self._pending.append(fr)

        if self._pending:
            frame = self._pending.popleft()
        else:
            frame = _silent_frame_48k()

        frame.pts = self._timestamp
        frame.sample_rate = sample_rate
        frame.time_base = fractions.Fraction(1, sample_rate)
        return frame
