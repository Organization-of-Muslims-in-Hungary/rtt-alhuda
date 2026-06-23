"""Periodic audio windowing, VAD gating, and OpenRouter transcription."""

import asyncio
import base64
import json
import time
import traceback
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession

from rtt_alhuda.audio_vad import is_speech_present
from rtt_alhuda.audio_wav import create_wav_bytes
from rtt_alhuda.audio_capture import stop_recording
from rtt_alhuda.config import (
    CHANNELS,
    CONTEXT_CHUNK_COUNT,
    OPENROUTER_RETRY_COUNT,
    OPENROUTER_TIMEOUT_SECONDS,
    PROCESSING_INTERVAL_SECONDS,
    REMOTE_MIC_TIMEOUT_SECONDS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
)
from rtt_alhuda.models import ChunkInfo, ServerSession
from rtt_alhuda.transcription_openrouter import send_chunk_to_openrouter
from rtt_alhuda.tts_openrouter import TtsLanguage, synthesize_speech_bytes
from rtt_alhuda.web_protocol import send_log, send_transcription


@dataclass
class _CycleTiming:
    """Mutable accumulator for per-cycle wall-clock timing breakdown."""

    cycle_start: float = 0.0
    queue_wait_ms: int = 0
    buffer_copy_ms: int = 0
    vad_ms: int = 0
    wav_encode_ms: int = 0
    api_ms: int = 0
    cycle_total_ms: int = 0
    audio_duration_ms: int = 0
    skipped: bool = False
    skip_reason: str = ""

    def snapshot(self) -> dict:
        """Return a JSON-serialisable dict of all timing phases."""
        return {
            "cycleTotalMs": self.cycle_total_ms,
            "queueWaitMs": self.queue_wait_ms,
            "bufferCopyMs": self.buffer_copy_ms,
            "vadMs": self.vad_ms,
            "wavEncodeMs": self.wav_encode_ms,
            "apiMs": self.api_ms,
            "audioDurationMs": self.audio_duration_ms,
            "skipped": self.skipped,
            "skipReason": self.skip_reason,
        }

    def summary_line(self) -> str:
        """One-line human-readable breakdown for server logs."""
        parts = [
            f"cycle={self.cycle_total_ms}ms",
            f"queue_wait={self.queue_wait_ms}ms",
            f"buffer_copy={self.buffer_copy_ms}ms",
            f"vad={self.vad_ms}ms",
            f"wav_enc={self.wav_encode_ms}ms",
            f"api={self.api_ms}ms",
            f"audio={self.audio_duration_ms}ms",
        ]
        if self.skipped:
            parts.append(f"SKIP({self.skip_reason})")
        return " | ".join(parts)


async def _enqueue_tts(session: ServerSession, http: ClientSession, text: str) -> None:
    """Fetch TTS audio and push one blob onto the debug TTS preview queue."""

    q = session.media_tts_queue
    if q is None:
        return
    try:
        lang: TtsLanguage = "hu" if session.media_tts_language == "hu" else "en"
        audio_bytes, _ = await synthesize_speech_bytes(http, text=text, language=lang)
        if audio_bytes:
            await q.put(audio_bytes)
    except Exception as exc:
        await send_log(session, f"TTS error: {exc}", "error")


async def _enqueue_tts_for_lang(
    session: ServerSession,
    http: ClientSession,
    text: str,
    lang: str,
) -> None:
    """Synth one chunk for satellite ``lang`` queue (en | hu)."""

    queues = session.tts_queues
    if not queues or lang not in queues:
        return
    q = queues[lang]
    try:
        voice_lang: TtsLanguage = "hu" if lang == "hu" else "en"
        audio_bytes, _ = await synthesize_speech_bytes(
            http, text=text, language=voice_lang
        )
        if audio_bytes:
            await q.put(audio_bytes)
    except Exception as exc:
        await send_log(session, f"TTS ({lang}) error: {exc}", "error")


async def _process_chunk(
    session: ServerSession,
    http: ClientSession,
    wav_b64: str,
    new_audio_start_sample: int,
    new_audio_end_sample: int,
    original_ar: str,
    original_en: str,
    original_hu: str,
    chunk_duration_seconds: float,
    timing: _CycleTiming,
    timeout_s: float = OPENROUTER_TIMEOUT_SECONDS,
):
    try:
        start_time = time.time()
        result = await send_chunk_to_openrouter(
            http,
            wav_b64,
            original_ar,
            original_en,
            original_hu,
            timeout_s=timeout_s,
        )
        timing.api_ms = int((time.time() - start_time) * 1000)

        new_ar = str(result.get("ar", ""))
        new_en = str(result.get("en", ""))
        new_hu = str(result.get("hu", ""))

        async with session.lock:
            session.chunk_history.append(
                ChunkInfo(
                    start_sample=new_audio_start_sample,
                    end_sample=new_audio_end_sample,
                    ar=new_ar,
                    en=new_en,
                    hu=new_hu,
                )
            )

        timing.audio_duration_ms = int(chunk_duration_seconds * 1000)
        timing.cycle_total_ms = int((time.monotonic() - timing.cycle_start) * 1000)

        message = {
            "type": "transcription",
            "ar": new_ar,
            "en": new_en,
            "hu": new_hu,
            "originalAr": original_ar,
            "originalEn": original_en,
            "originalHu": original_hu,
            "rawResponse": json.dumps(result),
            "originalAudioChunk": wav_b64,
            "processedChunks": 1,
            "windowSeconds": chunk_duration_seconds,
            "chunkDurationSeconds": chunk_duration_seconds,
            "latencyMs": timing.api_ms,
            "timing": timing.snapshot(),
        }
        await send_transcription(session, message)

        # Push to SSE /stream/text clients (same cycle, no polling delay).
        # The set lives on the session and is shared across WS reconnections;
        # synchronous list() snapshot is safe in single-threaded asyncio.
        sse_data = {"ar": new_ar, "en": new_en, "hu": new_hu}
        sse_payload = f"data: {json.dumps(sse_data, ensure_ascii=False)}\n\n".encode("utf-8")
        stale_sse: list = []
        for sse_resp in list(session.text_sse_clients):
            try:
                await sse_resp.write(sse_payload)
            except Exception:
                stale_sse.append(sse_resp)
                session.text_sse_clients.discard(sse_resp)
        if stale_sse:
            print(f"[SSE] Dropped {len(stale_sse)} stale SSE client(s)")

        langs_content = {
            "ar": new_ar.strip(),
            "en": new_en.strip(),
            "hu": new_hu.strip(),
        }

        # Debug audio preview: one TTS stream for subscribed debug WS clients
        tts_text = ""
        if session.media_tts_language == "hu":
            tts_text = langs_content["hu"]
        else:
            tts_text = langs_content["en"]

        if (
            tts_text
            and session.media_tts_queue is not None
            and session.tts_subscribers
        ):
            asyncio.create_task(
                _enqueue_tts(session, http, tts_text),
                name="tts-enqueue",
            )

        async with session.lock:
            satellite_langs = [
                lang
                for lang in ("en", "hu")
                if langs_content.get(lang)
                and len(session.tts_satellites.get(lang, ())) > 0
            ]
        for lang in satellite_langs:
            asyncio.create_task(
                _enqueue_tts_for_lang(session, http, langs_content[lang], lang),
                name=f"tts-satellite-{lang}",
            )

    except (asyncio.TimeoutError, ClientError) as exc:
        await send_log(
            session,
            f"Network error in chunk ({type(exc).__name__}): {exc}",
            "error",
        )
        raise
    except (RuntimeError, ValueError) as exc:
        await send_log(
            session,
            f"Error processing chunk ({type(exc).__name__}): {exc} "
            "(check server terminal for [OpenRouter] lines)",
            "error",
        )
    except Exception as exc:
        await send_log(
            session,
            f"Unexpected error processing chunk ({type(exc).__name__}): {exc}\n{traceback.format_exc()}",
            "error",
        )


async def process_audio_loop(session: ServerSession, http: ClientSession) -> None:
    """Periodically convert buffered audio into transcript and translation updates."""

    await send_log(
        session,
        f"Audio processing started (every {PROCESSING_INTERVAL_SECONDS}s, dynamic window)",
    )

    try:
        next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS

        while session.recording:
            t = _CycleTiming()

            # --- Phase: cadence wait (included in wall time) ---
            t.cycle_start = time.monotonic()
            wait_seconds = next_cycle_at - t.cycle_start
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
                t.queue_wait_ms = int((time.monotonic() - t.cycle_start) * 1000)
            else:
                t.queue_wait_ms = 0

            bytes_per_frame = CHANNELS * SAMPLE_WIDTH_BYTES

            # --- Phase: buffer copy ---
            _t0 = time.monotonic()
            async with session.lock:
                end_sample = session.total_samples_written
                chunk_pcm = b""
                new_audio_pcm = b""
                total_samples = 0

                if CONTEXT_CHUNK_COUNT > 0:
                    history_to_include = session.chunk_history[-CONTEXT_CHUNK_COUNT:]
                else:
                    history_to_include = []

                if history_to_include:
                    past_start_sample = history_to_include[0].start_sample
                    original_ar = " ".join(
                        c.ar for c in history_to_include if c.ar
                    ).strip()
                    original_en = " ".join(
                        c.en for c in history_to_include if c.en
                    ).strip()
                    original_hu = " ".join(
                        c.hu for c in history_to_include if c.hu
                    ).strip()
                else:
                    past_start_sample = session.last_chunk_end_sample
                    original_ar = ""
                    original_en = ""
                    original_hu = ""

                new_audio_start_sample = session.last_chunk_end_sample
                new_audio_end_sample = end_sample

                new_samples_count = new_audio_end_sample - new_audio_start_sample

                # Check if we have enough new audio, otherwise wait
                if new_samples_count < int(SAMPLE_RATE * PROCESSING_INTERVAL_SECONDS):
                    pass
                else:
                    start_sample = max(session.buffer_start_sample, past_start_sample)

                    start_offset_samples = start_sample - session.buffer_start_sample
                    end_offset_samples = end_sample - session.buffer_start_sample

                    start_byte = start_offset_samples * bytes_per_frame
                    end_byte = end_offset_samples * bytes_per_frame
                    chunk_pcm = bytes(session.pcm_buffer[start_byte:end_byte])

                    # VAD checks only the NEW audio (since last chunk), not context
                    safe_new_audio_start = max(session.buffer_start_sample, new_audio_start_sample)
                    new_start_offset = safe_new_audio_start - session.buffer_start_sample
                    new_start_byte = new_start_offset * bytes_per_frame
                    new_audio_pcm = bytes(session.pcm_buffer[new_start_byte:end_byte])

                    total_samples = end_sample - start_sample

            t.buffer_copy_ms = int((time.monotonic() - _t0) * 1000)

            if not chunk_pcm:
                t.cycle_total_ms = int((time.monotonic() - t.cycle_start) * 1000)
                t.skipped = True
                t.skip_reason = "no_new_audio"
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue

            # --- VAD check on exclusively new audio
            _t0 = time.monotonic()
            speech_detected = is_speech_present(new_audio_pcm)
            t.vad_ms = int((time.monotonic() - _t0) * 1000)

            if not speech_detected:
                t.cycle_total_ms = int((time.monotonic() - t.cycle_start) * 1000)
                t.skipped = True
                t.skip_reason = "silent_vad"
                t.audio_duration_ms = int((total_samples / SAMPLE_RATE) * 1000)
                await send_log(
                    session,
                    f"Silent audio chunk — skipped (wall {t.cycle_total_ms}ms, vad {t.vad_ms}ms)",
                    "info",
                    timing=t.snapshot(),
                )

                # Remote mic watchdog: auto-stop if no audio arrived for too long.
                if (
                    session.audio_source == "remote"
                    and session.last_remote_audio_ts > 0
                    and time.monotonic() - session.last_remote_audio_ts > REMOTE_MIC_TIMEOUT_SECONDS
                ):
                    await send_log(
                        session,
                        f"Remote mic timeout — no audio for {REMOTE_MIC_TIMEOUT_SECONDS}s, stopping recording",
                        "warn",
                    )
                    asyncio.create_task(stop_recording(session), name="remote-mic-timeout-stop")
                    session.recording = False
                    continue

                async with session.lock:
                    session.last_chunk_end_sample = new_audio_end_sample
                    # Add an empty chunk to history so the context window anchor moves forward.
                    # Without this, the anchor gets stuck and the 'context' audio window stretches endlessly.
                    session.chunk_history.append(
                        ChunkInfo(
                            start_sample=new_audio_start_sample,
                            end_sample=new_audio_end_sample,
                            ar="",
                            en="",
                            hu="",
                        )
                    )
                next_cycle_at = time.monotonic() + PROCESSING_INTERVAL_SECONDS
                continue
            # ------------------------

            # --- Phase: WAV + base64 encoding
            _t0 = time.monotonic()
            wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
            wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
            t.wav_encode_ms = int((time.monotonic() - _t0) * 1000)

            request_started_at = time.monotonic()

            chunk_ok = False
            context_reset = False
            for attempt in range(1 + OPENROUTER_RETRY_COUNT):
                try:
                    await _process_chunk(
                        session,
                        http,
                        wav_b64,
                        new_audio_start_sample,
                        new_audio_end_sample,
                        original_ar,
                        original_en,
                        original_hu,
                        chunk_duration_seconds=total_samples / SAMPLE_RATE,
                        timing=t,
                        timeout_s=OPENROUTER_TIMEOUT_SECONDS,
                    )
                    chunk_ok = True
                    break
                except (asyncio.TimeoutError, ClientError) as exc:
                    if attempt < OPENROUTER_RETRY_COUNT:
                        await send_log(
                            session,
                            f"OpenRouter timeout (attempt {attempt + 1}/{1 + OPENROUTER_RETRY_COUNT}), "
                            "retrying with accumulated audio",
                            "warn",
                        )
                        async with session.lock:
                            new_end = session.total_samples_written
                            new_audio_end_sample = new_end
                            end_offset = new_end - session.buffer_start_sample
                            end_byte = end_offset * bytes_per_frame
                            start_offset = start_sample - session.buffer_start_sample
                            start_byte = max(0, start_offset) * bytes_per_frame
                            if start_byte < len(session.pcm_buffer):
                                chunk_pcm = bytes(session.pcm_buffer[start_byte:end_byte])
                                total_samples = new_end - start_sample
                            else:
                                chunk_pcm = b""
                                total_samples = 0
                        if not chunk_pcm:
                            break
                        wav_bytes = create_wav_bytes(chunk_pcm, SAMPLE_RATE, CHANNELS)
                        wav_b64 = base64.b64encode(wav_bytes).decode("ascii")
                    else:
                        context_reset = True
                        async with session.lock:
                            session.last_chunk_end_sample = session.total_samples_written
                            session.chunk_history.clear()
                        await send_log(
                            session,
                            "OpenRouter unreachable — resetting context, continuing with fresh audio",
                            "error",
                        )

            if chunk_ok and not context_reset:
                async with session.lock:
                    session.last_chunk_end_sample = new_audio_end_sample

            next_cycle_at = request_started_at + PROCESSING_INTERVAL_SECONDS

    finally:
        await asyncio.shield(send_log(session, "Audio processing stopped"))
