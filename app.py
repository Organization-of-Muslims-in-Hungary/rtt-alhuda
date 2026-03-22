import os
import time
import io
import wave
import json
import re
from dotenv import load_dotenv
from google import genai
from google.genai import types
import sounddevice as sd


load_dotenv()

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
    # Guard against malformed streamed JSON fragments leaking into text fields.
    markers = ['{"transcription"', '{"translation"', '{ "transcription"', '{ "translation"']
    for marker in markers:
        idx = cleaned.find(marker)
        if idx > 0:
            cleaned = cleaned[:idx].strip()
            break
    return cleaned


def extract_json_object(text: str):
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    best = None
    best_score = -1
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start == -1:
            break
        try:
            candidate, end = decoder.raw_decode(text[start:])
            if isinstance(candidate, dict):
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
                if score >= best_score:
                    best = candidate
                    best_score = score
            idx = start + max(end, 1)
        except json.JSONDecodeError:
            idx = start + 1

    return best


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def record_microphone_chunk(duration_seconds: int = 5, sample_rate: int = 16000):
    frames = int(duration_seconds * sample_rate)
    with sd.RawInputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="int16",
    ) as stream:
        audio_bytes, overflowed = stream.read(frames)
        if overflowed:
            print("Warning: audio input overflowed during recording.")

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(audio_bytes)

    return buffer.getvalue()


def build_client_and_config():
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing API key. Add GEMINI_API_KEY or GOOGLE_API_KEY to your .env file."
        )

    client = genai.Client(
        api_key=api_key,
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

    return client, generate_content_config


def transcribe_chunk(
    client,
    generate_content_config,
    chunk_index: int,
    duration_seconds: int = 5,
    sample_rate: int = 16000,
    log_fn=None,
):
    def emit(message: str):
        print(message, flush=True)
        if log_fn:
            log_fn(message)

    chunk_start = time.perf_counter()
    emit(f"\n[chunk {chunk_index}] Recording...")

    record_start = time.perf_counter()
    audio_bytes = record_microphone_chunk(
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
    )
    record_seconds = time.perf_counter() - record_start
    emit(
        f"[chunk {chunk_index}] Recorded {len(audio_bytes)} bytes in {record_seconds:.2f}s"
    )

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
    def emit(message: str):
        print(message, flush=True)
        if log_fn:
            log_fn(message)

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

    emit(f"[chunk {chunk_index}] Sending audio to model...")
    request_start = time.perf_counter()

    response_text = ""
    stream_event_count = 0
    first_text_at = None
    for response_chunk in client.models.generate_content_stream(
        model=MODEL_NAME,
        contents=contents,
        config=generate_content_config,
    ):
        stream_event_count += 1
        if response_chunk.text:
            if first_text_at is None:
                first_text_at = time.perf_counter()
                emit(
                    f"[chunk {chunk_index}] First response text after {first_text_at - request_start:.2f}s"
                )
            response_text += response_chunk.text

    receive_done = time.perf_counter()
    receive_seconds = receive_done - request_start
    emit(
        f"[chunk {chunk_index}] Response received in {receive_seconds:.2f}s "
        f"({stream_event_count} stream events, {len(response_text)} chars)"
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
            payload["stable_transcription"] = legacy_transcription
        if not payload["stable_translation"] and not payload["unstable_translation_tail"]:
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
    else:
        # Keep raw for debugging only; do not pollute live transcript with malformed JSON.
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
    emit(
        f"[chunk {chunk_index}] Processing took {process_seconds:.2f}s | "
        f"Total chunk time {chunk_seconds:.2f}s"
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

    return payload


def generate():
    client, generate_content_config = build_client_and_config()

    chunk_index = 1
    while True:
        payload = transcribe_chunk(
            client,
            generate_content_config,
            chunk_index=chunk_index,
            duration_seconds=5,
            sample_rate=16000,
        )
        if payload.get("transcription") or payload.get("translation"):
            print(
                json.dumps(
                    {
                        "transcription": payload.get("transcription", ""),
                        "translation": payload.get("translation", ""),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(payload.get("raw", ""))

        print()
        chunk_index += 1

if __name__ == "__main__":
    print('started ..')
    start = time.time()
    generate()
    end = time.time()
    print(f"\nTime taken: {end - start} seconds")