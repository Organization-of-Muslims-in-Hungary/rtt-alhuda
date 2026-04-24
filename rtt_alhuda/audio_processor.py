"""Periodic audio windowing, VAD gating, and OpenRouter transcription."""

import asyncio
import base64
import json
import time
import traceback

from aiohttp import ClientError, ClientSession

from rtt_alhuda.audio_vad import is_speech_present
from rtt_alhuda.audio_wav import create_wav_bytes
from rtt_alhuda.config import (
    CHANNELS,
    CONTEXT_CHUNK_COUNT,
    PROCESSING_INTERVAL_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from rtt_alhuda.models import ChunkInfo, ClientState
from rtt_alhuda.transcription_openrouter import send_chunk_to_openrouter
from rtt_alhuda.web_protocol import send_log, send_transcription


async def _process_chunk(
    client: ClientState,
    http: ClientSession,
    wav_b64: str,
    new_audio_start_sample: int,
    new_audio_end_sample: int,
    original_transcription: str,
    original_translation: str,
    chunk_duration_seconds: float,
):
    try:
        start_time = time.time()
        result = await send_chunk_to_openrouter(
            http,
            wav_b64,
            original_transcription,
            original_translation,
        )
        latency_ms = int((time.time() - start_time) * 1000)

        new_transcription = str(result.get("new_additional_transcription", ""))
        new_translation = str(result.get("new_additional_translation", ""))

        async with client.lock:
            client.chunk_history.append(
                ChunkInfo(
                    start_sample=new_audio_start_sample,
                    end_sample=new_audio_end_sample,
                    transcription=new_transcription,
                    translation=new_translation,
                )
            )

        message = {
            "type": "transcription",
            "transcription": new_transcription,
            "translation": new_translation,
            "originalTranscription": original_transcription,
            "originalTranslation": original_translation,
            "rawResponse": json.dumps(result),
            "originalAudioChunk": wav_b64,
            "processedChunks": 1,
            "windowSeconds": chunk_duration_seconds,
            "chunkDurationSeconds": chunk_duration_seconds,
            "latencyMs": latency_ms,
        }
        await send_transcription(client, message)

    except (asyncio.TimeoutError, ClientError, RuntimeError, ValueError) as exc:
        await send_log(client, f"Error processing chunk ({type(exc).__name__}): {exc}", "error")
    except Exception as exc:
        await send_log(
            client,
            f"Unexpected error processing chunk ({type(exc).__name__}): {exc}\n{traceback.format_exc()}",
            "error",
        )


async def process_audio_loop(client: ClientState, http: ClientSession) -> None:
    """Periodically convert buffered audio into transcript and translation updates."""

    await send_log(
        client,
        f"Audio processing started (every {PROCESSING_INTERVAL_SECONDS}s, dynamic window)",
    )

    try:
        next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS

        while client.recording and not client.ws.closed:
            wait_seconds = next_cycle_at - time.monotonic()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

            async with client.lock:
                end_sample = client.total_samples_written
                chunk_pcm = b""
                new_audio_pcm = b""
                total_samples = 0

                if CONTEXT_CHUNK_COUNT > 0:
                    history_to_include = client.chunk_history[-CONTEXT_CHUNK_COUNT:]
                else:
                    history_to_include = []

                if history_to_include:
                    past_start_sample = history_to_include[0].start_sample
                    original_transcription = " ".join(
                        c.transcription for c in history_to_include if c.transcription
                    ).strip()
                    original_translation = " ".join(
                        c.translation for c in history_to_include if c.translation
                    ).strip()
                else:
                    past_start_sample = client.last_chunk_end_sample
                    original_transcription = ""
                    original_translation = ""

                new_audio_start_sample = client.last_chunk_end_sample
                new_audio_end_sample = end_sample

                new_samples_count = new_audio_end_sample - new_audio_start_sample

                # Check if we have enough new audio, otherwise wait
                if new_samples_count < int(SAMPLE_RATE * PROCESSING_INTERVAL_SECONDS):
                    pass
                else:
                    start_sample = max(client.buffer_start_sample, past_start_sample)
                    start_offset_samples = start_sample - client.buffer_start_sample
                    end_offset_samples = end_sample - client.buffer_start_sample

                    start_byte = start_offset_samples * bytes_per_frame
                    end_byte = end_offset_samples * bytes_per_frame
                    chunk_pcm = bytes(client.pcm_buffer[start_byte:end_byte])

                    # For VAD, we only want to check the NEW audio, not the historical overlap
                    safe_new_audio_start = max(client.buffer_start_sample, new_audio_start_sample)
                    new_start_offset = safe_new_audio_start - client.buffer_start_sample
                    new_start_byte = new_start_offset * bytes_per_frame
                    new_audio_pcm = bytes(client.pcm_buffer[new_start_byte:end_byte])

                    total_samples = end_sample - start_sample

            if not chunk_pcm:
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue

            # --- WebRTC VAD check on exclusively new audio
            if not is_speech_present(new_audio_pcm):
                await send_log(client, "Silent audio chunk detected by VAD, skipping LLM request.")
                async with client.lock:
                    client.last_chunk_end_sample = new_audio_end_sample
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue
            # ------------------------

            wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
            wav_b64 = base64.b64encode(wav_bytes).decode("ascii")

            request_started_at = time.monotonic()

            await _process_chunk(
                client,
                http,
                wav_b64,
                new_audio_start_sample,
                new_audio_end_sample,
                original_transcription,
                original_translation,
                chunk_duration_seconds=total_samples / SAMPLE_RATE,
            )

            async with client.lock:
                client.last_chunk_end_sample = new_audio_end_sample

            # Keep a request-start-based cadence: every PROCESSING_INTERVAL_SECONDS
            # from request start, or immediately after slower responses.
            next_cycle_at = request_started_at + PROCESSING_INTERVAL_SECONDS

    finally:
        await send_log(client, "Audio processing stopped")
