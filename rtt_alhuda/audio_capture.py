"""Server-side microphone capture into the client PCM buffer."""

import asyncio

from rtt_alhuda.config import (
    CHANNELS,
    FRAME_CHUNK_SECONDS,
    MAX_BUFFER_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from rtt_alhuda.models import ClientState
from rtt_alhuda.web_protocol import send_log


async def capture_microphone_loop(client: ClientState) -> None:
    """Continuously read microphone audio into the client's rolling buffer."""

    try:
        import sounddevice as sd
    except ImportError:
        await send_log(
            client,
            "Missing dependency: sounddevice. Install with: pip install sounddevice",
            "error",
        )
        client.recording = False
        return

    try:
        default_in = sd.query_devices(kind="input")
        if default_in.get("max_input_channels", 0) < CHANNELS:
            await send_log(client, "Default audio device does not support audio capture.", "error")
            client.recording = False
            return
    except Exception as exc:
        await send_log(client, f"Warning: Could not query input device ({exc})", "warn")

    frame_chunk = int(SAMPLE_RATE * FRAME_CHUNK_SECONDS)
    max_buffer_samples = int(SAMPLE_RATE * MAX_BUFFER_SECONDS)
    bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

    try:
        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=frame_chunk,
        ) as stream:
            await send_log(client, "Microphone capture started")
            while client.recording:
                data, overflowed = await asyncio.to_thread(stream.read, frame_chunk)
                if overflowed:
                    await send_log(client, "Input overflow detected", "warn")

                pcm_bytes = bytes(data)
                sample_count = len(pcm_bytes) // bytes_per_frame
                async with client.lock:
                    client.pcm_buffer.extend(pcm_bytes)
                    client.total_samples_written += sample_count

                    available_samples = (
                        client.total_samples_written - client.buffer_start_sample
                    )
                    overflow_samples = available_samples - max_buffer_samples
                    if overflow_samples > 0:
                        overflow_bytes = overflow_samples * bytes_per_frame
                        del client.pcm_buffer[:overflow_bytes]
                        client.buffer_start_sample += overflow_samples

                mic_q = client.media_mic_queue
                if mic_q is not None:
                    try:
                        mic_q.put_nowait(pcm_bytes)
                    except asyncio.QueueFull:
                        pass
                orig_q = client.original_pcm_queue
                if orig_q is not None:
                    try:
                        orig_q.put_nowait(pcm_bytes)
                    except asyncio.QueueFull:
                        pass
    except Exception as exc:
        await send_log(client, f"Microphone error: {exc}", "error")
        client.recording = False
    finally:
        client.recording = False
        await send_log(client, "Microphone capture stopped")
