import io
import json
import logging
import os
import re
import sys
import time
import traceback
import wave
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import sounddevice as sd
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOG_LEVEL = os.environ.get("RTT_LOG_LEVEL", "DEBUG").upper()
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


logger = setup_logger("rtt.app", "app.log")

PROMPT = (
    "Transcribe only what is clearly audible in THIS audio chunk in Arabic and provide "
    "its English translation. If no clear speech is audible, return empty strings. "
    "Do not continue prior text unless it is actually heard in this chunk. "
    "Do not include timestamps, metadata, or extra commentary."
)
MODEL_NAME = "gemini-2.5-flash-lite"
MAX_OUTPUT_TOKENS = 260
TEMPERATURE = 0.0
TOP_P = 0.2
TOP_K = 20
TAIL_CONTEXT_WORDS = 15
TIMESTAMP_PATTERN = re.compile(r"\b\d{2}:\d{2}\b")


def clean_text(text: str) -> str:
    cleaned = TIMESTAMP_PATTERN.sub("", text)
    cleaned = re.sub(r"\n\s*\n+", "\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def clean_model_field(text: str) -> str:
    cleaned = clean_text(text)
    markers = ['{"transcription"', '{"translation"', '{ "transcription"', '{ "translation"']
    for marker in markers:
        idx = cleaned.find(marker)
        if idx > 0:
            logger.debug("Truncating malformed JSON fragment at position %d", idx)
            cleaned = cleaned[:idx].strip()
            break
    return cleaned


def extract_json_object(text: str):
    text = text.strip()
    if not text:
        logger.debug("extract_json_object: empty input")
        return None

    try:
        result = json.loads(text)
        logger.debug("extract_json_object: direct JSON parse succeeded")
        return result
    except json.JSONDecodeError as e:
        logger.debug("extract_json_object: direct parse failed (%s), trying recovery", e)

    decoder = json.JSONDecoder()
    best = None
    best_score = -1
    idx = 0
    candidates_found = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            candidate, end = decoder.raw_decode(text[start:])
            if isinstance(candidate, dict):
                candidates_found += 1
                score = 0
                if candidate.get("stable_transcription"):
                    score += 3
                if candidate.get("stable_translation"):
                    score += 3
                if candidate.get("transcription"):
                    score += 2
                if candidate.get("translation"):
                    score += 2
                if candidate.get("unstable_transcription_tail") is not None:
                    score += 1
                if candidate.get("unstable_translation_tail") is not None:
                    score += 1
                logger.debug(
                    "extract_json_object: candidate %d at pos %d, score=%d",
                    candidates_found, start, score
                )
                if score >= best_score:
                    best = candidate
                    best_score = score
            idx = start + max(end, 1)
        except json.JSONDecodeError:
            idx = start + 1

    if best is not None:
        logger.debug(
            "extract_json_object: selected candidate with score=%d from %d candidates",
            best_score, candidates_found
        )
    else:
        logger.warning("extract_json_object: no valid JSON object found in response")

    return best


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def record_microphone_chunk(duration_seconds: int = 5, sample_rate: int = 16000):
    frames = int(duration_seconds * sample_rate)
    logger.debug(
        "Recording microphone chunk: duration=%ds, sample_rate=%d, frames=%d",
        duration_seconds, sample_rate, frames
    )

    try:
        with sd.RawInputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
        ) as stream:
            audio_bytes, overflowed = stream.read(frames)
            if overflowed:
                logger.warning("Audio input overflowed during recording")
    except Exception as e:
        logger.error("Failed to record from microphone: %s", e)
        logger.debug("Microphone error traceback:\n%s", traceback.format_exc())
        raise

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)

    wav_bytes = buffer.getvalue()
    logger.debug("Recorded %d bytes of WAV audio", len(wav_bytes))
    return wav_bytes


def build_client_and_config():
    logger.info("Building Gemini client and configuration")
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        logger.error("Missing API key in environment variables")
        raise RuntimeError(
            "Missing API key. Add GEMINI_API_KEY or GOOGLE_API_KEY to your .env file."
        )

    logger.debug("API key found (length=%d), creating client", len(api_key))
    client = genai.Client(
        api_key=api_key,
    )

    logger.debug(
        "Configuring model: %s, max_tokens=%d, temp=%.2f, top_p=%.2f, top_k=%d",
        MODEL_NAME, MAX_OUTPUT_TOKENS, TEMPERATURE, TOP_P, TOP_K
    )
    generate_content_config = types.GenerateContentConfig(
        system_instruction=[
            types.Part.from_text(text="""You are a realtime translator for Arabic speech.
Rules:
1) Output only words that are clearly present in the current audio chunk.
2) If audio is silence/noise/unclear, return empty strings for all text fields.
3) Never invent continuation from prior chunks unless those words are audible now.
4) Avoid loops/repetition.
5) For chunk boundaries, split output into stable text and an unstable tail likely to be revised by next chunk.
6) If you can correct the previous chunk tail from new context, return revised_prev_* fields."""),
        ],
        max_output_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        top_p=TOP_P,
        top_k=TOP_K,
        thinking_config=types.ThinkingConfig(
            thinking_budget=0,
        ),
        response_mime_type="application/json",
        response_schema=genai.types.Schema(
            type = genai.types.Type.OBJECT,
            required = [
                "transcription",
                "translation",
                "stable_transcription",
                "unstable_transcription_tail",
                "stable_translation",
                "unstable_translation_tail",
            ],
            properties = {
                "transcription": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "translation": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "stable_transcription": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "unstable_transcription_tail": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "revised_prev_transcription_tail": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "stable_translation": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "unstable_translation_tail": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "revised_prev_translation_tail": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
                "tail_confidence": genai.types.Schema(
                    type = genai.types.Type.NUMBER,
                ),
                "dedupe_anchor": genai.types.Schema(
                    type = genai.types.Type.STRING,
                ),
            },
        ),
    )

    logger.info("Gemini client and config built successfully")
    return client, generate_content_config


def transcribe_chunk(
    client,
    generate_content_config,
    chunk_index: int,
    duration_seconds: int = 5,
    sample_rate: int = 16000,
    log_fn=None,
):
    def emit(message: str, level: str = "info"):
        log_method = getattr(logger, level, logger.info)
        log_method("[chunk %d] %s", chunk_index, message)
        if log_fn:
            log_fn(message)

    chunk_start = time.perf_counter()
    logger.info("=== Chunk %d: Starting transcription ===", chunk_index)
    emit("Recording...")

    record_start = time.perf_counter()
    try:
        audio_bytes = record_microphone_chunk(
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
        )
    except Exception as e:
        logger.error("[chunk %d] Recording failed: %s", chunk_index, e)
        raise

    record_seconds = time.perf_counter() - record_start
    emit(f"Recorded {len(audio_bytes)} bytes in {record_seconds:.2f}s")

    return process_audio_chunk(
        client,
        generate_content_config,
        chunk_index=chunk_index,
        audio_bytes=audio_bytes,
        record_seconds=record_seconds,
        chunk_start=chunk_start,
        log_fn=log_fn,
    )


def process_audio_chunk(
    client,
    generate_content_config,
    chunk_index: int,
    audio_bytes: bytes,
    prior_tail_transcription: str = "",
    prior_tail_translation: str = "",
    prev_chunk_index: int = 0,
    overlap_seconds: float = 0.0,
    record_seconds: float = 0.0,
    chunk_start=None,
    queue_wait_seconds: float = 0.0,
    log_fn=None,
):
    def emit(message: str, level: str = "info"):
        log_method = getattr(logger, level, logger.info)
        log_method("[chunk %d] %s", chunk_index, message)
        if log_fn:
            log_fn(f"[chunk {chunk_index}] {message}")

    logger.debug(
        "[chunk %d] process_audio_chunk called: audio_bytes=%d, prev_chunk=%d, overlap=%.2fs, queue_wait=%.3fs",
        chunk_index, len(audio_bytes), prev_chunk_index, overlap_seconds, queue_wait_seconds
    )
    if prior_tail_transcription:
        logger.debug("[chunk %d] prior_tail_transcription: %s", chunk_index, prior_tail_transcription[:100])
    if prior_tail_translation:
        logger.debug("[chunk %d] prior_tail_translation: %s", chunk_index, prior_tail_translation[:100])

    context_text = (
        "Realtime boundary context:\n"
        f"- previous_chunk_index: {prev_chunk_index}\n"
        f"- overlap_seconds: {overlap_seconds:.2f}\n"
        f"- prior_tail_transcription: {prior_tail_transcription}\n"
        f"- prior_tail_translation: {prior_tail_translation}\n"
        "Use prior tails only for boundary correction, not for continuation.\n"
        "Output must include stable_* fields and unstable_*_tail fields.\n"
        "If this chunk has no clear speech, return all text fields empty.\n"
        "Keep unstable tails short (usually 1-3 words)."
    )

    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=PROMPT),
                types.Part.from_text(text=context_text),
                types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav"),
            ],
        ),
    ]

    emit("Sending audio to model...")
    request_start = time.perf_counter()

    response_text = ""
    stream_event_count = 0
    first_text_at = None

    try:
        for response_chunk in client.models.generate_content_stream(
            model=MODEL_NAME,
            contents=contents,
            config=generate_content_config,
        ):
            stream_event_count += 1
            if response_chunk.text:
                if first_text_at is None:
                    first_text_at = time.perf_counter()
                    ttfb = first_text_at - request_start
                    emit(f"First response text after {ttfb:.2f}s (TTFB)")
                    logger.debug("[chunk %d] Time to first byte: %.3fs", chunk_index, ttfb)
                response_text += response_chunk.text
    except Exception as e:
        logger.error("[chunk %d] Model API error: %s", chunk_index, e)
        logger.debug("[chunk %d] API error traceback:\n%s", chunk_index, traceback.format_exc())
        raise

    receive_done = time.perf_counter()
    receive_seconds = receive_done - request_start
    emit(f"Response received in {receive_seconds:.2f}s ({stream_event_count} stream events, {len(response_text)} chars)")
    logger.debug(
        "[chunk %d] Stream complete: events=%d, chars=%d, receive_time=%.3fs",
        chunk_index, stream_event_count, len(response_text), receive_seconds
    )

    process_start = time.perf_counter()
    payload = {
        "transcription": "",
        "translation": "",
        "stable_transcription": "",
        "unstable_transcription_tail": "",
        "revised_prev_transcription_tail": "",
        "stable_translation": "",
        "unstable_translation_tail": "",
        "revised_prev_translation_tail": "",
        "tail_confidence": 0.0,
        "dedupe_anchor": "",
        "raw": response_text,
    }

    parsed = extract_json_object(response_text)
    if parsed is not None:
        logger.debug("[chunk %d] JSON parsed successfully, extracting fields", chunk_index)
        payload["stable_transcription"] = clean_model_field(parsed.get("stable_transcription", ""))
        payload["unstable_transcription_tail"] = clean_model_field(
            parsed.get("unstable_transcription_tail", "")
        )
        payload["revised_prev_transcription_tail"] = clean_model_field(
            parsed.get("revised_prev_transcription_tail", "")
        )
        payload["stable_translation"] = clean_model_field(parsed.get("stable_translation", ""))
        payload["unstable_translation_tail"] = clean_model_field(
            parsed.get("unstable_translation_tail", "")
        )
        payload["revised_prev_translation_tail"] = clean_model_field(
            parsed.get("revised_prev_translation_tail", "")
        )
        payload["tail_confidence"] = safe_float(parsed.get("tail_confidence", 0.0), 0.0)
        payload["dedupe_anchor"] = clean_model_field(parsed.get("dedupe_anchor", ""))

        legacy_transcription = clean_model_field(parsed.get("transcription", ""))
        legacy_translation = clean_model_field(parsed.get("translation", ""))
        if not payload["stable_transcription"] and not payload["unstable_transcription_tail"]:
            logger.debug("[chunk %d] Using legacy transcription field", chunk_index)
            payload["stable_transcription"] = legacy_transcription
        if not payload["stable_translation"] and not payload["unstable_translation_tail"]:
            logger.debug("[chunk %d] Using legacy translation field", chunk_index)
            payload["stable_translation"] = legacy_translation

        payload["transcription"] = " ".join(
            part for part in [
                payload["stable_transcription"],
                payload["unstable_transcription_tail"],
            ] if part
        ).strip()
        payload["translation"] = " ".join(
            part for part in [
                payload["stable_translation"],
                payload["unstable_translation_tail"],
            ] if part
        ).strip()

        if not payload["dedupe_anchor"]:
            words = payload["stable_transcription"].split()
            payload["dedupe_anchor"] = " ".join(words[-3:]) if words else ""

        logger.info(
            "[chunk %d] Transcription: %s | Translation: %s",
            chunk_index,
            payload["transcription"][:80] + "..." if len(payload["transcription"]) > 80 else payload["transcription"],
            payload["translation"][:80] + "..." if len(payload["translation"]) > 80 else payload["translation"],
        )
    else:
        logger.warning("[chunk %d] Failed to parse JSON response, raw length=%d", chunk_index, len(response_text))
        payload["raw"] = clean_text(response_text)
        payload["stable_transcription"] = ""
        payload["stable_translation"] = ""
        payload["transcription"] = ""
        payload["translation"] = ""

    process_seconds = time.perf_counter() - process_start
    if chunk_start is not None:
        chunk_seconds = time.perf_counter() - chunk_start
    else:
        chunk_seconds = record_seconds + queue_wait_seconds + receive_seconds + process_seconds

    emit(f"Processing took {process_seconds:.2f}s | Total chunk time {chunk_seconds:.2f}s")
    logger.debug(
        "[chunk %d] Timing breakdown: record=%.3fs, queue_wait=%.3fs, receive=%.3fs, process=%.3fs, total=%.3fs",
        chunk_index, record_seconds, queue_wait_seconds, receive_seconds, process_seconds, chunk_seconds
    )

    payload["chunk_index"] = chunk_index
    payload["record_seconds"] = round(record_seconds, 3)
    payload["queue_wait_seconds"] = round(queue_wait_seconds, 3)
    payload["receive_seconds"] = round(receive_seconds, 3)
    payload["process_seconds"] = round(process_seconds, 3)
    payload["total_seconds"] = round(chunk_seconds, 3)
    payload["stream_event_count"] = stream_event_count
    payload["audio_bytes"] = len(audio_bytes)
    payload["prior_tail_transcription"] = prior_tail_transcription
    payload["prior_tail_translation"] = prior_tail_translation
    payload["prev_chunk_index"] = prev_chunk_index
    payload["overlap_seconds"] = round(overlap_seconds, 3)

    logger.debug("[chunk %d] === Chunk processing complete ===", chunk_index)
    return payload


def generate():
    logger.info("Starting continuous transcription loop")
    client, generate_content_config = build_client_and_config()

    chunk_index = 1
    session_start = time.time()

    try:
        while True:
            try:
                payload = transcribe_chunk(
                    client,
                    generate_content_config,
                    chunk_index=chunk_index,
                    duration_seconds=5,
                    sample_rate=16000,
                )
                if payload.get("transcription") or payload.get("translation"):
                    output = json.dumps(
                        {
                            "transcription": payload.get("transcription", ""),
                            "translation": payload.get("translation", ""),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    print(output)
                else:
                    raw = payload.get("raw", "")
                    if raw:
                        logger.debug("[chunk %d] No transcription, raw: %s", chunk_index, raw[:200])
                    print(raw)

                print()
                chunk_index += 1

            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error("[chunk %d] Error during transcription: %s", chunk_index, e)
                logger.debug("Chunk error traceback:\n%s", traceback.format_exc())
                chunk_index += 1
                time.sleep(1)

    except KeyboardInterrupt:
        logger.info("Received interrupt signal, stopping...")
    finally:
        session_duration = time.time() - session_start
        logger.info(
            "Session ended: %d chunks processed in %.1f seconds (avg %.2fs/chunk)",
            chunk_index - 1, session_duration,
            session_duration / max(1, chunk_index - 1)
        )


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("RTT-Alhuda CLI starting at %s", datetime.now().isoformat())
    logger.info("Log level: %s, Log directory: %s", LOG_LEVEL, LOG_DIR)
    logger.info("=" * 60)

    start = time.time()
    try:
        generate()
    except Exception as e:
        logger.critical("Fatal error: %s", e)
        logger.debug("Fatal error traceback:\n%s", traceback.format_exc())
        sys.exit(1)
    finally:
        end = time.time()
        logger.info("Total runtime: %.1f seconds", end - start)