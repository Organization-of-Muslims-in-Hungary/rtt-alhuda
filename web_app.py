import io
import json
import math
import queue
import threading
import time
import wave
from array import array
from collections import deque

import sounddevice as sd
from flask import Flask, Response, jsonify, render_template

from app import build_client_and_config, process_audio_chunk


app = Flask(__name__)


class TranscriptionController:
    def __init__(self):
        self.recording = False
        self.chunk_index = 1
        self.client = None
        self.generate_content_config = None

        self.chunk_duration_seconds = 10.0
        self.overlap_ratio = 0.5
        self.stride_seconds = self.chunk_duration_seconds * (1 - self.overlap_ratio)
        self.sample_rate = 16000
        self.silence_rms_threshold = 0.0005

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

        self.recorder_thread = threading.Thread(target=self._recorder_worker, daemon=True)
        self.processor_thread = threading.Thread(target=self._processor_worker, daemon=True)
        self.recorder_thread.start()
        self.processor_thread.start()

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.subscribers.add(q)
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

    def publish(self, event_type, payload):
        message = {"type": event_type, "payload": payload}
        with self.lock:
            subscribers = list(self.subscribers)
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
        with self.lock:
            self.recording = True
            self.transcription_committed = ""
            self.transcription_provisional = ""
            self.translation_committed = ""
            self.translation_provisional = ""
            self.last_tail_transcription = ""
            self.last_tail_translation = ""
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
        with self.lock:
            self.recording = False
        cleared = self._clear_audio_queue()
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
            self.publish("log", {"message": f"Cleared {cleared} queued chunk(s)."})
        self.publish("log", {"message": "Recording stopped from web UI."})

    def _ensure_client(self):
        if self.client is None or self.generate_content_config is None:
            self.client, self.generate_content_config = build_client_and_config()
            self.publish("log", {"message": "Gemini client initialized."})

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
        frames_per_chunk = int(self.chunk_duration_seconds * self.sample_rate)
        stride_frames = max(1, int(self.stride_seconds * self.sample_rate))
        bytes_per_frame = 2
        window_bytes = frames_per_chunk * bytes_per_frame
        stride_bytes = stride_frames * bytes_per_frame

        while True:
            with self.lock:
                is_recording = self.recording

            if not is_recording:
                time.sleep(0.1)
                continue

            next_chunk_start_seconds = 0.0
            try:
                self.publish("log", {"message": "Microphone stream opened."})
                pcm_window = bytearray()
                with sd.RawInputStream(samplerate=self.sample_rate, channels=1, dtype="int16") as stream:
                    while True:
                        with self.lock:
                            if not self.recording:
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
                            except queue.Full:
                                dropped = self.audio_queue.get_nowait()
                                self.audio_queue.task_done()
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

                self.publish("log", {"message": "Microphone stream closed."})
            except Exception as exc:
                self.publish("error", {"message": str(exc)})
                self.publish(
                    "log",
                    {
                        "message": (
                            f"[chunk {item.get('chunk_index', '?')}] Model processing error; continuing with next chunk."
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
        while True:
            try:
                item = self.audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                self._ensure_client()
                queue_wait_seconds = time.perf_counter() - item["captured_at"]
                processing_meta = self._set_audio_meta(
                    item["chunk_index"],
                    state="processing",
                    queue_wait_seconds=round(queue_wait_seconds, 3),
                    queue_size=self.audio_queue.qsize(),
                )
                self.publish(
                    "audio_chunk",
                    {
                        "chunk_index": item["chunk_index"],
                        "audio_url": self._audio_url(item["chunk_index"]),
                        **processing_meta,
                    },
                )

                payload = process_audio_chunk(
                    self.client,
                    self.generate_content_config,
                    chunk_index=item["chunk_index"],
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

                done_meta = self._set_audio_meta(
                    item["chunk_index"],
                    state="processed",
                    queue_wait_seconds=payload.get("queue_wait_seconds", 0),
                    receive_seconds=payload.get("receive_seconds", 0),
                    process_seconds=payload.get("process_seconds", 0),
                    total_seconds=payload.get("total_seconds", 0),
                    start_ts=item.get("start_ts", 0.0),
                    end_ts=item.get("end_ts", 0.0),
                    overlap_seconds=item.get("overlap_seconds", 0.0),
                )

                payload["audio_url"] = self._audio_url(item["chunk_index"])
                self.publish("chunk", payload)
                self.publish(
                    "audio_chunk",
                    {
                        "chunk_index": item["chunk_index"],
                        "audio_url": self._audio_url(item["chunk_index"]),
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
                self.publish("error", {"message": str(exc)})
                with self.lock:
                    self.recording = False
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


@app.get("/")
def index():
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


@app.post("/start")
def start_recording():
    controller.start()
    return jsonify({"ok": True, "recording": True})


@app.post("/stop")
def stop_recording():
    controller.stop()
    return jsonify({"ok": True, "recording": False})


@app.get("/audio/<int:chunk_index>.wav")
def audio_chunk(chunk_index: int):
    audio_bytes = controller.get_audio(chunk_index)
    if audio_bytes is None:
        return Response("Not Found", status=404)

    return Response(
        audio_bytes,
        mimetype="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/stream")
def stream():
    def event_stream():
        q = controller.subscribe()
        try:
            while True:
                try:
                    message = q.get(timeout=15)
                    event_type = message["type"]
                    payload = message["payload"]
                    yield (
                        f"event: {event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                except queue.Empty:
                    yield ": ping\n\n"
        except Exception as exc:
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
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
