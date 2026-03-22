import io
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import traceback
import wave
from array import array
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sounddevice as sd
from flask import Flask, Response, jsonify, render_template, request

from app import build_client_and_config, process_audio_chunk, LOG_DIR, LOG_LEVEL

# ---------------------------------------------------------------------------
# Logging Configuration for Web App
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
LOG_BACKUP_COUNT = 5


def setup_logger(name: str, log_file: str | None = None) -> logging.Logger:
    """Configure and return a logger with console and optional file handlers."""
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))
    logger.handlers.clear()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.DEBUG))
    logger.addHandler(console_handler)

    if log_file:
        file_path = LOG_DIR / log_file
        file_handler = RotatingFileHandler(
            file_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)

    return logger


logger = setup_logger("rtt.web", "web_app.log")

app = Flask(__name__)

# Suppress Flask's default request logging in production
werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.setLevel(logging.WARNING)


class TranscriptionController:
    def __init__(self):
        logger.info("Initializing TranscriptionController")
        self.recording = False
        self.chunk_index = 1
        self.client = None
        self.generate_content_config = None

        self.chunk_duration_seconds = 10.0
        self.overlap_ratio = 0.5
        self.stride_seconds = self.chunk_duration_seconds * (1 - self.overlap_ratio)
        self.sample_rate = 16000
        self.silence_rms_threshold = 0.0005

        logger.debug(
            "Audio config: chunk_duration=%.1fs, overlap_ratio=%.2f, stride=%.1fs, sample_rate=%d",
            self.chunk_duration_seconds, self.overlap_ratio, self.stride_seconds, self.sample_rate
        )

        self.audio_queue = queue.Queue(maxsize=24)
        self.max_debug_audio_chunks = 80
        self.audio_store = {}
        self.audio_order = deque()
        self.audio_meta = {}

        self.transcription_committed = ""
        self.transcription_provisional = ""
        self.translation_committed = ""
        self.translation_provisional = ""
        self.last_tail_transcription = ""
        self.last_tail_translation = ""
        self.tail_context_words = 15

        self.subscribers = set()
        self.lock = threading.Lock()

        # Statistics
        self.stats = {
            "chunks_recorded": 0,
            "chunks_processed": 0,
            "chunks_skipped_silence": 0,
            "chunks_dropped": 0,
            "total_audio_bytes": 0,
            "total_process_time": 0.0,
            "session_start": None,
            "errors": 0,
        }

        self.recorder_thread = threading.Thread(target=self._recorder_worker, daemon=True, name="RecorderThread")
        self.processor_thread = threading.Thread(target=self._processor_worker, daemon=True, name="ProcessorThread")
        self.recorder_thread.start()
        self.processor_thread.start()
        logger.info("Worker threads started: recorder=%s, processor=%s",
                    self.recorder_thread.name, self.processor_thread.name)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subscribers.add(q)
            subscriber_count = len(self.subscribers)
        logger.info("New SSE subscriber connected (total: %d)", subscriber_count)
        self.publish(
            "status",
            {
                "recording": self.recording,
                "chunk_index": self.chunk_index,
                "queue_size": self.audio_queue.qsize(),
                "overlap_ratio": self.overlap_ratio,
                "stride_seconds": self.stride_seconds,
            },
        )
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)
            subscriber_count = len(self.subscribers)
        logger.info("SSE subscriber disconnected (remaining: %d)", subscriber_count)

    def publish(self, event_type, payload):
        message = {"type": event_type, "payload": payload}
        with self.lock:
            subscribers = list(self.subscribers)
        if event_type not in ("status", "log"):
            logger.debug("Publishing event '%s' to %d subscribers", event_type, len(subscribers))
        for q in subscribers:
            q.put(message)

    def _audio_url(self, chunk_index: int) -> str:
        return f"/audio/{chunk_index}.wav"

    def _store_audio(self, chunk_index: int, audio_bytes: bytes):
        with self.lock:
            self.audio_store[chunk_index] = audio_bytes
            self.audio_order.append(chunk_index)
            while len(self.audio_order) > self.max_debug_audio_chunks:
                oldest = self.audio_order.popleft()
                self.audio_store.pop(oldest, None)
                self.audio_meta.pop(oldest, None)

    def get_audio(self, chunk_index: int):
        with self.lock:
            return self.audio_store.get(chunk_index)

    def _set_audio_meta(self, chunk_index: int, **fields):
        with self.lock:
            current = self.audio_meta.get(chunk_index, {})
            current.update(fields)
            self.audio_meta[chunk_index] = current
            return dict(current)

    def _pcm_to_wav(self, pcm_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()

    def _pcm_rms(self, pcm_bytes: bytes) -> float:
        samples = array("h")
        samples.frombytes(pcm_bytes)
        if not samples:
            return 0.0
        squares = 0.0
        for s in samples:
            squares += float(s) * float(s)
        return math.sqrt(squares / len(samples)) / 32768.0

    def _append_with_word_overlap(self, base_text: str, new_text: str) -> str:
        base_words = base_text.split()
        new_words = new_text.split()
        if not new_words:
            return base_text.strip()
        if not base_words:
            return " ".join(new_words).strip()

        max_overlap = min(8, len(base_words), len(new_words))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            if base_words[-size:] == new_words[:size]:
                overlap = size
                break

        merged_words = base_words + new_words[overlap:]
        return self._collapse_adjacent_repetition(" ".join(merged_words).strip())

    def _collapse_adjacent_repetition(self, text: str, max_phrase_words: int = 8) -> str:
        words = text.split()
        if len(words) < 2:
            return text.strip()

        changed = True
        while changed:
            changed = False
            i = 0
            output = []
            while i < len(words):
                collapsed = False
                max_size = min(max_phrase_words, (len(words) - i) // 2)
                for size in range(max_size, 0, -1):
                    first = words[i : i + size]
                    second = words[i + size : i + (2 * size)]
                    if first == second:
                        output.extend(first)
                        i += 2 * size
                        changed = True
                        collapsed = True
                        break
                if not collapsed:
                    output.append(words[i])
                    i += 1
            words = output

        return " ".join(words).strip()

    def _sanitize_revision_tail(self, current_tail: str, candidate_tail: str) -> str:
        candidate_words = candidate_tail.split()
        current_words = current_tail.split()
        if not candidate_words:
            return ""

        # Prevent model from rewriting large spans when only tail edits are expected.
        max_allowed = max(4, len(current_words) + 4)
        if len(candidate_words) > max_allowed:
            return current_tail

        return self._collapse_adjacent_repetition(candidate_tail)

    def _apply_chunk_merge(self, payload):
        revised_applied = False
        revised_transcription = payload.get("revised_prev_transcription_tail", "").strip()
        revised_translation = payload.get("revised_prev_translation_tail", "").strip()

        if revised_transcription and self.transcription_provisional:
            self.transcription_provisional = self._sanitize_revision_tail(
                self.transcription_provisional,
                revised_transcription,
            )
            revised_applied = True
        if revised_translation and self.translation_provisional:
            self.translation_provisional = self._sanitize_revision_tail(
                self.translation_provisional,
                revised_translation,
            )
            revised_applied = True

        if self.transcription_provisional:
            self.transcription_committed = self._append_with_word_overlap(
                self.transcription_committed,
                self.transcription_provisional,
            )
        if self.translation_provisional:
            self.translation_committed = self._append_with_word_overlap(
                self.translation_committed,
                self.translation_provisional,
            )

        self.transcription_provisional = ""
        self.translation_provisional = ""

        stable_transcription = self._collapse_adjacent_repetition(
            payload.get("stable_transcription", "").strip()
        )
        stable_translation = self._collapse_adjacent_repetition(
            payload.get("stable_translation", "").strip()
        )
        unstable_transcription_tail = self._collapse_adjacent_repetition(
            payload.get("unstable_transcription_tail", "").strip()
        )
        unstable_translation_tail = self._collapse_adjacent_repetition(
            payload.get("unstable_translation_tail", "").strip()
        )

        if not stable_transcription and not unstable_transcription_tail:
            stable_transcription = payload.get("transcription", "").strip()
        if not stable_translation and not unstable_translation_tail:
            stable_translation = payload.get("translation", "").strip()

        if stable_transcription:
            self.transcription_committed = self._append_with_word_overlap(
                self.transcription_committed,
                stable_transcription,
            )
        if stable_translation:
            self.translation_committed = self._append_with_word_overlap(
                self.translation_committed,
                stable_translation,
            )

        self.transcription_provisional = unstable_transcription_tail
        self.translation_provisional = unstable_translation_tail

        merged_transcription = self._append_with_word_overlap(
            self.transcription_committed,
            self.transcription_provisional,
        )
        merged_translation = self._append_with_word_overlap(
            self.translation_committed,
            self.translation_provisional,
        )

        combined_transcription = merged_transcription.split()
        combined_translation = merged_translation.split()
        self.last_tail_transcription = " ".join(combined_transcription[-self.tail_context_words:])
        self.last_tail_translation = " ".join(combined_translation[-self.tail_context_words:])

        payload["merged_transcription"] = merged_transcription
        payload["merged_translation"] = merged_translation
        payload["committed_transcription"] = self.transcription_committed
        payload["committed_translation"] = self.translation_committed
        payload["provisional_transcription"] = self.transcription_provisional
        payload["provisional_translation"] = self.translation_provisional
        payload["revised_prev_applied"] = revised_applied
        payload["merge_fallback_used"] = (
            (not payload.get("stable_transcription", "").strip() and bool(stable_transcription))
            or (not payload.get("stable_translation", "").strip() and bool(stable_translation))
        )
        payload["dedupe_sanitized"] = True
        return payload

    def start(self):
        logger.info("=== Recording session started ===")
        with self.lock:
            self.recording = True
            self.transcription_committed = ""
            self.transcription_provisional = ""
            self.translation_committed = ""
            self.translation_provisional = ""
            self.last_tail_transcription = ""
            self.last_tail_translation = ""
            self.stats["session_start"] = time.time()
            self.stats["chunks_recorded"] = 0
            self.stats["chunks_processed"] = 0
            self.stats["chunks_skipped_silence"] = 0
            self.stats["chunks_dropped"] = 0
            self.stats["total_audio_bytes"] = 0
            self.stats["total_process_time"] = 0.0
            self.stats["errors"] = 0

        self.publish(
            "status",
            {
                "recording": True,
                "chunk_index": self.chunk_index,
                "queue_size": self.audio_queue.qsize(),
                "overlap_ratio": self.overlap_ratio,
                "stride_seconds": self.stride_seconds,
            },
        )
        self.publish("log", {"message": "Recording started from web UI."})

    def stop(self):
        logger.info("=== Recording session stopping ===")
        session_duration = 0.0
        with self.lock:
            self.recording = False
            if self.stats["session_start"]:
                session_duration = time.time() - self.stats["session_start"]

        cleared = self._clear_audio_queue()

        logger.info(
            "Session stats: duration=%.1fs, recorded=%d, processed=%d, skipped=%d, dropped=%d, errors=%d",
            session_duration,
            self.stats["chunks_recorded"],
            self.stats["chunks_processed"],
            self.stats["chunks_skipped_silence"],
            self.stats["chunks_dropped"],
            self.stats["errors"],
        )
        if self.stats["chunks_processed"] > 0:
            avg_process = self.stats["total_process_time"] / self.stats["chunks_processed"]
            logger.info("Average process time: %.2fs per chunk", avg_process)

        self.publish(
            "status",
            {
                "recording": False,
                "chunk_index": self.chunk_index,
                "queue_size": self.audio_queue.qsize(),
                "overlap_ratio": self.overlap_ratio,
                "stride_seconds": self.stride_seconds,
            },
        )
        if cleared:
            logger.debug("Cleared %d queued chunk(s) on stop", cleared)
            self.publish("log", {"message": f"Cleared {cleared} queued chunk(s)."})
        self.publish("log", {"message": "Recording stopped from web UI."})

    def _ensure_client(self):
        if self.client is None or self.generate_content_config is None:
            logger.info("Initializing Gemini client...")
            try:
                self.client, self.generate_content_config = build_client_and_config()
                logger.info("Gemini client initialized successfully")
                self.publish("log", {"message": "Gemini client initialized."})
            except Exception as e:
                logger.error("Failed to initialize Gemini client: %s", e)
                raise

    def _clear_audio_queue(self):
        cleared = 0
        while True:
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.task_done()
                cleared += 1
            except queue.Empty:
                return cleared

    def _recorder_worker(self):
        logger.info("Recorder worker thread started")
        frames_per_chunk = int(self.chunk_duration_seconds * self.sample_rate)
        stride_frames = max(1, int(self.stride_seconds * self.sample_rate))
        bytes_per_frame = 2
        window_bytes = frames_per_chunk * bytes_per_frame
        stride_bytes = stride_frames * bytes_per_frame

        logger.debug(
            "Recorder config: frames_per_chunk=%d, stride_frames=%d, window_bytes=%d, stride_bytes=%d",
            frames_per_chunk, stride_frames, window_bytes, stride_bytes
        )

        while True:
            with self.lock:
                is_recording = self.recording

            if not is_recording:
                time.sleep(0.1)
                continue

            next_chunk_start_seconds = 0.0
            item = {}
            try:
                logger.info("Opening microphone stream (sample_rate=%d)", self.sample_rate)
                self.publish("log", {"message": "Microphone stream opened."})
                pcm_window = bytearray()
                with sd.RawInputStream(samplerate=self.sample_rate, channels=1, dtype="int16") as stream:
                    logger.debug("Microphone stream opened successfully")
                    while True:
                        with self.lock:
                            if not self.recording:
                                logger.debug("Recording flag cleared, exiting recording loop")
                                break
                            chunk_index = self.chunk_index

                        self.publish(
                            "log",
                            {
                                "message": (
                                    f"\n[chunk {chunk_index}] Recording stride ({self.stride_seconds:.2f}s)..."
                                )
                            },
                        )
                        record_start = time.perf_counter()
                        raw_buffer, overflowed = stream.read(stride_frames)
                        record_seconds = time.perf_counter() - record_start
                        pcm_window.extend(bytes(raw_buffer))

                        if overflowed:
                            logger.warning("[chunk %d] Audio input overflow detected", chunk_index)
                            self.publish(
                                "log",
                                {
                                    "message": (
                                        f"[chunk {chunk_index}] Warning: input overflow while recording."
                                    )
                                },
                            )

                        if len(pcm_window) < window_bytes:
                            continue

                        chunk_pcm = bytes(pcm_window[:window_bytes])
                        if len(pcm_window) > stride_bytes:
                            del pcm_window[:stride_bytes]
                        else:
                            pcm_window.clear()

                        audio_bytes = self._pcm_to_wav(chunk_pcm)
                        chunk_rms = self._pcm_rms(chunk_pcm)
                        chunk_end_seconds = next_chunk_start_seconds + self.chunk_duration_seconds

                        item = {
                            "chunk_index": chunk_index,
                            "audio_bytes": audio_bytes,
                            "record_seconds": record_seconds,
                            "captured_at": time.perf_counter(),
                            "prev_chunk_index": max(0, chunk_index - 1),
                            "overlap_seconds": self.chunk_duration_seconds * self.overlap_ratio,
                            "stride_seconds": self.stride_seconds,
                            "start_ts": round(next_chunk_start_seconds, 3),
                            "end_ts": round(chunk_end_seconds, 3),
                            "rms": round(chunk_rms, 6),
                        }
                        next_chunk_start_seconds += self.stride_seconds

                        with self.lock:
                            self.stats["chunks_recorded"] += 1
                            self.stats["total_audio_bytes"] += len(audio_bytes)

                        logger.debug(
                            "[chunk %d] Recorded: bytes=%d, rms=%.6f, window=%.2f-%.2fs",
                            chunk_index, len(audio_bytes), chunk_rms, item["start_ts"], item["end_ts"]
                        )

                        self._store_audio(chunk_index, audio_bytes)
                        audio_meta = self._set_audio_meta(
                            chunk_index,
                            state="recorded",
                            audio_bytes=len(audio_bytes),
                            record_seconds=round(record_seconds, 3),
                            rms=item["rms"],
                            start_ts=item["start_ts"],
                            end_ts=item["end_ts"],
                            overlap_seconds=item["overlap_seconds"],
                        )
                        self.publish(
                            "audio_chunk",
                            {
                                "chunk_index": chunk_index,
                                "audio_url": self._audio_url(chunk_index),
                                **audio_meta,
                            },
                        )

                        if chunk_rms < self.silence_rms_threshold:
                            logger.debug("[chunk %d] Skipped (silence): rms=%.6f < threshold=%.6f",
                                        chunk_index, chunk_rms, self.silence_rms_threshold)
                            with self.lock:
                                self.stats["chunks_skipped_silence"] += 1

                            silence_meta = self._set_audio_meta(
                                chunk_index,
                                state="silence_skipped",
                                queue_size=self.audio_queue.qsize(),
                            )
                            self.publish(
                                "audio_chunk",
                                {
                                    "chunk_index": chunk_index,
                                    "audio_url": self._audio_url(chunk_index),
                                    **silence_meta,
                                },
                            )
                            self.publish(
                                "log",
                                {
                                    "message": (
                                        f"[chunk {chunk_index}] Skipped model call (silence RMS={chunk_rms:.4f} < "
                                        f"{self.silence_rms_threshold:.4f})."
                                    )
                                },
                            )
                        else:
                            try:
                                self.audio_queue.put_nowait(item)
                                logger.debug("[chunk %d] Queued for processing (queue_size=%d)",
                                            chunk_index, self.audio_queue.qsize())
                            except queue.Full:
                                dropped = self.audio_queue.get_nowait()
                                self.audio_queue.task_done()
                                with self.lock:
                                    self.stats["chunks_dropped"] += 1
                                logger.warning("[chunk %d] Queue full, dropping chunk %d",
                                              chunk_index, dropped["chunk_index"])

                                dropped_meta = self._set_audio_meta(
                                    dropped["chunk_index"],
                                    state="dropped",
                                )
                                self.publish(
                                    "audio_chunk",
                                    {
                                        "chunk_index": dropped["chunk_index"],
                                        "audio_url": self._audio_url(dropped["chunk_index"]),
                                        **dropped_meta,
                                    },
                                )
                                self.publish(
                                    "log",
                                    {
                                        "message": (
                                            f"Queue full. Dropping oldest chunk {dropped['chunk_index']} "
                                            "to keep recording realtime."
                                        )
                                    },
                                )
                                self.audio_queue.put_nowait(item)

                            queued_meta = self._set_audio_meta(
                                chunk_index,
                                state="queued",
                                queue_size=self.audio_queue.qsize(),
                            )
                            self.publish(
                                "audio_chunk",
                                {
                                    "chunk_index": chunk_index,
                                    "audio_url": self._audio_url(chunk_index),
                                    **queued_meta,
                                },
                            )

                        self.publish(
                            "log",
                            {
                                "message": (
                                    f"[chunk {chunk_index}] Recorded {len(audio_bytes)} bytes in "
                                    f"{record_seconds:.2f}s (rms={chunk_rms:.4f}, queue={self.audio_queue.qsize()}, "
                                    f"window={item['start_ts']:.2f}-{item['end_ts']:.2f}s, "
                                    f"overlap={item['overlap_seconds']:.2f}s)"
                                )
                            },
                        )

                        with self.lock:
                            self.chunk_index += 1
                        self.publish(
                            "status",
                            {
                                "recording": True,
                                "chunk_index": self.chunk_index,
                                "queue_size": self.audio_queue.qsize(),
                                "overlap_ratio": self.overlap_ratio,
                                "stride_seconds": self.stride_seconds,
                            },
                        )

                logger.info("Microphone stream closed normally")
                self.publish("log", {"message": "Microphone stream closed."})
            except Exception as exc:
                logger.error("Recorder worker error: %s", exc)
                logger.debug("Recorder error traceback:\n%s", traceback.format_exc())
                with self.lock:
                    self.stats["errors"] += 1

                self.publish("error", {"message": str(exc)})
                self.publish(
                    "log",
                    {
                        "message": (
                            f"[chunk {item.get('chunk_index', '?')}] Recording error; continuing with next chunk."
                        )
                    },
                )
                self.publish(
                    "status",
                    {
                        "recording": self.recording,
                        "chunk_index": self.chunk_index,
                        "queue_size": self.audio_queue.qsize(),
                        "overlap_ratio": self.overlap_ratio,
                        "stride_seconds": self.stride_seconds,
                    },
                )

    def _processor_worker(self):
        logger.info("Processor worker thread started")
        while True:
            try:
                item = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            chunk_index = item["chunk_index"]
            process_start = time.perf_counter()

            try:
                self._ensure_client()
                queue_wait_seconds = time.perf_counter() - item["captured_at"]

                logger.debug(
                    "[chunk %d] Starting processing (queue_wait=%.3fs, remaining_queue=%d)",
                    chunk_index, queue_wait_seconds, self.audio_queue.qsize()
                )

                processing_meta = self._set_audio_meta(
                    chunk_index,
                    state="processing",
                    queue_wait_seconds=round(queue_wait_seconds, 3),
                    queue_size=self.audio_queue.qsize(),
                )
                self.publish(
                    "audio_chunk",
                    {
                        "chunk_index": chunk_index,
                        "audio_url": self._audio_url(chunk_index),
                        **processing_meta,
                    },
                )

                payload = process_audio_chunk(
                    self.client,
                    self.generate_content_config,
                    chunk_index=chunk_index,
                    audio_bytes=item["audio_bytes"],
                    prior_tail_transcription=self.last_tail_transcription,
                    prior_tail_translation=self.last_tail_translation,
                    prev_chunk_index=item.get("prev_chunk_index", 0),
                    overlap_seconds=item.get("overlap_seconds", 0.0),
                    record_seconds=item["record_seconds"],
                    queue_wait_seconds=queue_wait_seconds,
                    log_fn=lambda msg: self.publish("log", {"message": msg}),
                )
                payload["start_ts"] = item.get("start_ts", 0.0)
                payload["end_ts"] = item.get("end_ts", 0.0)
                payload["stride_seconds"] = item.get("stride_seconds", self.stride_seconds)

                payload = self._apply_chunk_merge(payload)

                process_time = time.perf_counter() - process_start
                with self.lock:
                    self.stats["chunks_processed"] += 1
                    self.stats["total_process_time"] += process_time

                logger.info(
                    "[chunk %d] Processed in %.2fs: transcription=%d chars, translation=%d chars",
                    chunk_index, process_time,
                    len(payload.get("transcription", "")),
                    len(payload.get("translation", ""))
                )

                done_meta = self._set_audio_meta(
                    chunk_index,
                    state="processed",
                    queue_wait_seconds=payload.get("queue_wait_seconds", 0),
                    receive_seconds=payload.get("receive_seconds", 0),
                    process_seconds=payload.get("process_seconds", 0),
                    total_seconds=payload.get("total_seconds", 0),
                    start_ts=item.get("start_ts", 0.0),
                    end_ts=item.get("end_ts", 0.0),
                    overlap_seconds=item.get("overlap_seconds", 0.0),
                )

                payload["audio_url"] = self._audio_url(chunk_index)
                self.publish("chunk", payload)
                self.publish(
                    "audio_chunk",
                    {
                        "chunk_index": chunk_index,
                        "audio_url": self._audio_url(chunk_index),
                        **done_meta,
                    },
                )
                self.publish(
                    "status",
                    {
                        "recording": self.recording,
                        "chunk_index": self.chunk_index,
                        "queue_size": self.audio_queue.qsize(),
                        "overlap_ratio": self.overlap_ratio,
                        "stride_seconds": self.stride_seconds,
                    },
                )
            except Exception as exc:
                logger.error("[chunk %d] Processing error: %s", chunk_index, exc)
                logger.debug("[chunk %d] Processing error traceback:\n%s", chunk_index, traceback.format_exc())

                with self.lock:
                    self.stats["errors"] += 1
                    self.recording = False

                self.publish("error", {"message": str(exc)})
                self.publish(
                    "status",
                    {
                        "recording": False,
                        "chunk_index": self.chunk_index,
                        "queue_size": self.audio_queue.qsize(),
                        "overlap_ratio": self.overlap_ratio,
                        "stride_seconds": self.stride_seconds,
                    },
                )
            finally:
                self.audio_queue.task_done()


controller = TranscriptionController()


@app.before_request
def log_request():
    if request.path not in ("/stream", "/status"):
        logger.debug("Request: %s %s", request.method, request.path)


@app.after_request
def log_response(response):
    if request.path not in ("/stream", "/status", "/audio"):
        logger.debug("Response: %s %s -> %d", request.method, request.path, response.status_code)
    return response


@app.get("/")
def index():
    logger.debug("Serving index page")
    return render_template("index.html")


@app.get("/status")
def status():
    return jsonify(
        {
            "recording": controller.recording,
            "chunk_index": controller.chunk_index,
            "queue_size": controller.audio_queue.qsize(),
            "overlap_ratio": controller.overlap_ratio,
            "stride_seconds": controller.stride_seconds,
        }
    )


@app.get("/stats")
def stats():
    """Return detailed statistics about the transcription session."""
    with controller.lock:
        session_duration = 0.0
        if controller.stats["session_start"]:
            session_duration = time.time() - controller.stats["session_start"]

        avg_process_time = 0.0
        if controller.stats["chunks_processed"] > 0:
            avg_process_time = controller.stats["total_process_time"] / controller.stats["chunks_processed"]

        return jsonify({
            "session_duration_seconds": round(session_duration, 2),
            "chunks_recorded": controller.stats["chunks_recorded"],
            "chunks_processed": controller.stats["chunks_processed"],
            "chunks_skipped_silence": controller.stats["chunks_skipped_silence"],
            "chunks_dropped": controller.stats["chunks_dropped"],
            "total_audio_bytes": controller.stats["total_audio_bytes"],
            "total_audio_mb": round(controller.stats["total_audio_bytes"] / (1024 * 1024), 2),
            "avg_process_time_seconds": round(avg_process_time, 3),
            "errors": controller.stats["errors"],
            "queue_size": controller.audio_queue.qsize(),
            "subscribers": len(controller.subscribers),
        })


@app.post("/start")
def start_recording():
    logger.info("Start recording requested via API")
    controller.start()
    return jsonify({"ok": True, "recording": True})


@app.post("/stop")
def stop_recording():
    logger.info("Stop recording requested via API")
    controller.stop()
    return jsonify({"ok": True, "recording": False})


@app.get("/audio/<int:chunk_index>.wav")
def audio_chunk(chunk_index: int):
    audio_bytes = controller.get_audio(chunk_index)
    if audio_bytes is None:
        logger.debug("Audio chunk %d not found", chunk_index)
        return Response("Not Found", status=404)

    logger.debug("Serving audio chunk %d (%d bytes)", chunk_index, len(audio_bytes))
    return Response(
        audio_bytes,
        mimetype="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/stream")
def stream():
    def event_stream():
        q = controller.subscribe()
        events_sent = 0
        try:
            while True:
                try:
                    message = q.get(timeout=15)
                    event_type = message["type"]
                    payload = message["payload"]
                    events_sent += 1
                    yield (
                        f"event: {event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                except queue.Empty:
                    yield ": ping\n\n"
        except GeneratorExit:
            logger.debug("SSE stream closed by client after %d events", events_sent)
        except Exception as exc:
            logger.error("SSE stream error: %s", exc)
            error_payload = {"message": str(exc)}
            yield f"event: error\ndata: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
        finally:
            controller.unsubscribe(q)

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("RTT-Alhuda Web Server starting at %s", datetime.now().isoformat())
    logger.info("Log level: %s, Log directory: %s", LOG_LEVEL, LOG_DIR)
    logger.info("Server: http://127.0.0.1:5021")
    logger.info("=" * 60)

    try:
        app.run(host="127.0.0.1", port=5021, debug=False, threaded=True)
    except KeyboardInterrupt:
        logger.info("Server shutdown requested")
    except Exception as e:
        logger.critical("Server failed: %s", e)
        logger.debug("Server error traceback:\n%s", traceback.format_exc())
    finally:
        logger.info("Server stopped")
