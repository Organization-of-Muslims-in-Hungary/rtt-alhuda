"""Server-side microphone capture into the session PCM buffer."""

import asyncio

from rtt_alhuda.config import (
    CHANNELS,
    FRAME_CHUNK_SECONDS,
    MAX_BUFFER_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from rtt_alhuda.models import ServerSession
from rtt_alhuda.web_protocol import send_log


async def feed_remote_audio(session: ServerSession, pcm_bytes: bytes) -> None:
    """Inject PCM data received from a remote (browser) mic into the session
    buffer and fan-out queues — same logic as the native capture loop."""

    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES
    sample_count = len(pcm_bytes) // bytes_per_frame
    max_buffer_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)

    async with session.lock:
        session.pcm_buffer.extend(pcm_bytes)
        session.total_samples_written += sample_count

        available_samples = (
            session.total_samples_written - session.buffer_start_sample
        )
        overflow_samples = available_samples - max_buffer_samples
        if overflow_samples > 0:
            overflow_bytes = overflow_samples * bytes_per_frame
            del session.pcm_buffer[:overflow_bytes]
            session.buffer_start_sample += overflow_samples

    mic_q = session.media_mic_queue
    if mic_q is not None:
        try:
            mic_q.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass
    orig_q = session.original_pcm_queue
    if orig_q is not None:
        try:
            orig_q.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            pass


async def capture_microphone_loop(session: ServerSession) -> None:
    """Continuously read microphone audio into the session's rolling buffer."""

    try:
        import sounddevice as sd
    except ImportError:
        await send_log(
            session,
            "Missing dependency: sounddevice. Install with: pip install sounddevice",
            "error",
        )
        session.recording = False
        return

    frame_chunk = int(SAMPLE_RATE * FRAME_CHUNK_SECONDS)
    max_buffer_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

    try:
        with sd.RawInputStream(
            device="pulse",
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=frame_chunk,
        ) as stream:
            await send_log(session, "Microphone capture started")
            while session.recording:
                data, overflowed = await asyncio.to_thread(stream.read, frame_chunk)
                if overflowed:
                    await send_log(session, "Input overflow detected", "warn")

                pcm_bytes = bytes(data)
                sample_count = len(pcm_bytes) // bytes_per_frame
                async with session.lock:
                    session.pcm_buffer.extend(pcm_bytes)
                    session.total_samples_written += sample_count

                    available_samples = (
                        session.total_samples_written - session.buffer_start_sample
                    )
                    overflow_samples = available_samples - max_buffer_samples
                    if overflow_samples > 0:
                        overflow_bytes = overflow_samples * bytes_per_frame
                        del session.pcm_buffer[:overflow_bytes]
                        session.buffer_start_sample += overflow_samples

                mic_q = session.media_mic_queue
                if mic_q is not None:
                    try:
                        mic_q.put_nowait(pcm_bytes)
                    except asyncio.QueueFull:
                        pass
                orig_q = session.original_pcm_queue
                if orig_q is not None:
                    try:
                        orig_q.put_nowait(pcm_bytes)
                    except asyncio.QueueFull:
                        pass
    except Exception as exc:
        await send_log(session, f"Microphone error: {exc}", "error")
        session.recording = False
    finally:
        session.recording = False
        await send_log(session, "Microphone capture stopped")
