#!/usr/bin/env python3
"""Chunked synchronous transcription for the Qwen-Audio-3.x ASR sync model.

The filetrans model needs a public URL that Alibaba can pull quickly. A general
file host behind a CDN cannot guarantee that: large uploads time out on the
provider side long after the upload itself looked healthy. The synchronous model
accepts the audio inline as Base64 instead, so this module normalizes the audio,
splits it into sub-five-minute chunks, transcribes them concurrently, and
stitches the word timestamps back into one continuous stream.

Everything here is deliberately free of network and provider state: the HTTP
call is injected, so chunk planning, request construction, stitching, and the
integrity gate are all testable offline.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

SYNC_ENDPOINT_SUFFIX = "/api/v1/services/aigc/multimodal-generation/generation"
DEFAULT_MAX_CHUNK_SECONDS = 270.0
DEFAULT_CONCURRENCY = 2
DEFAULT_RETRIES = 3
DEFAULT_MP3_KBPS = 16
SILENCE_WINDOW_SECONDS = 30.0
MIN_TAIL_SECONDS = 5.0
MIN_CHUNK_SECONDS = 5.0
COVERAGE_TOLERANCE_SECONDS = 1.0
OVERLAP_TOLERANCE_SECONDS = 0.05
SENTENCE_TERMINATORS = (".", "!", "?", "。", "！", "？", "…")


@dataclass(frozen=True)
class ChunkWindow:
    """One contiguous slice of the normalized audio, in seconds."""

    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for audio normalization but was not found on PATH.")


def require_ffprobe() -> None:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is required to measure audio duration but was not found on PATH.")


def probe_duration(path: Path) -> float:
    require_ffprobe()
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"Could not read the audio duration of {path}: {result.stderr.strip()}") from None
    if duration <= 0:
        raise RuntimeError(f"Audio duration is not positive: {path}")
    return duration


def normalize_audio(source: Path, target: Path, *, bitrate_kbps: int = DEFAULT_MP3_KBPS) -> Path:
    """Convert to mono 16 kHz MP3 so every chunk is small and predictable."""
    require_ffmpeg()
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        f"{int(bitrate_kbps)}k",
        "-f",
        "mp3",
        str(target),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(f"Audio normalization failed for {source}: {result.stderr.strip()[-400:]}")
    return target


def detect_silences(audio: Path, *, noise_db: int = -35, min_seconds: float = 0.5) -> list[tuple[float, float]]:
    """Return silence intervals in seconds, used only to pick natural cut points."""
    require_ffmpeg()
    command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(audio),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_seconds}",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    stderr = result.stderr or ""
    if result.returncode != 0:
        # Silence detection is an optimization: a failure must not block the run.
        return []
    silences: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        if "silence_start:" in line:
            try:
                pending_start = float(line.split("silence_start:", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pending_start = None
        elif "silence_end:" in line and pending_start is not None:
            try:
                end = float(line.split("silence_end:", 1)[1].strip().split()[0])
            except (IndexError, ValueError):
                pending_start = None
                continue
            if end > pending_start:
                silences.append((pending_start, end))
            pending_start = None
    return silences


def pick_silence_cut(silences: list[tuple[float, float]], target: float, window: float) -> float | None:
    """Choose the silence midpoint at or before `target` inside the window.

    Cutting at or before the target keeps every chunk inside the model's limit;
    the closest midpoint wins so chunks stay close to the target length.
    """
    best: float | None = None
    for start, end in silences:
        midpoint = (start + end) / 2
        if target - window <= midpoint <= target:
            if best is None or midpoint > best:
                best = midpoint
    return best


def plan_chunk_windows(
    duration: float,
    max_seconds: float,
    silences: list[tuple[float, float]] | None = None,
    *,
    window: float = SILENCE_WINDOW_SECONDS,
    min_tail: float = MIN_TAIL_SECONDS,
) -> list[ChunkWindow]:
    """Split `duration` into consecutive windows of at most `max_seconds`.

    Cuts prefer a silence midpoint inside the window before each target, and
    fall back to a hard cut. Windows never overlap, so no word is transcribed
    twice. A trailing sliver is merged into the previous window when it fits.
    """
    if duration <= 0:
        raise ValueError("duration must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    if duration <= max_seconds:
        return [ChunkWindow(0, 0.0, duration)]

    silences = silences or []
    cuts: list[float] = []
    position = 0.0
    while duration - position > max_seconds:
        target = position + max_seconds
        cut = pick_silence_cut(silences, target, window)
        if cut is None or cut - position < MIN_CHUNK_SECONDS:
            cut = target
        cuts.append(round(cut, 3))
        position = cut

    # Merge a trailing sliver into the previous window when the result still fits.
    while len(cuts) >= 2 and duration - cuts[-1] < min_tail:
        if duration - cuts[-2] <= max_seconds:
            cuts.pop()
        else:
            break

    bounds = [0.0, *cuts, duration]
    return [
        ChunkWindow(index, bounds[index], bounds[index + 1])
        for index in range(len(bounds) - 1)
    ]


def extract_chunk(audio: Path, window: ChunkWindow, target: Path, *, bitrate_kbps: int = DEFAULT_MP3_KBPS) -> Path:
    require_ffmpeg()
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{window.start:.3f}",
        "-t",
        f"{window.duration:.3f}",
        "-i",
        str(audio),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        f"{int(bitrate_kbps)}k",
        "-f",
        "mp3",
        str(target),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(
            f"Chunk {window.index + 1} audio extraction failed: {result.stderr.strip()[-400:]}"
        )
    return target


def build_sync_request(*, model: str, audio_bytes: bytes, hotwords: dict[str, int] | None = None) -> dict:
    """Build the synchronous multimodal-generation request body.

    The audio travels inline as a data URL, so no public link is involved.
    """
    if not audio_bytes:
        raise ValueError("audio payload is empty")
    encoded = base64.b64encode(audio_bytes).decode("ascii")
    parameters: dict[str, object] = {"format": "mp3", "sample_rate": "16000"}
    if hotwords:
        parameters["vocabulary"] = dict(hotwords)
    return {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {"data": f"data:audio/mpeg;base64,{encoded}"},
                        }
                    ],
                }
            ]
        },
        "parameters": parameters,
    }


def parse_sync_response(payload: dict) -> tuple[str, list[dict], float]:
    """Return (text, words in milliseconds, reported duration in seconds).

    The sync model answers under `output.sentence` (or `output.sentences`), not
    under the usual multimodal `choices` array.
    """
    output = payload.get("output") or {}
    sentences: list[dict] = []
    single = output.get("sentence")
    if isinstance(single, dict):
        sentences.append(single)
    extra = output.get("sentences")
    if isinstance(extra, list):
        sentences.extend(item for item in extra if isinstance(item, dict))

    words: list[dict] = []
    texts: list[str] = []
    for sentence in sentences:
        sentence_text = str(sentence.get("text") or "").strip()
        if sentence_text:
            texts.append(sentence_text)
        for raw_word in sentence.get("words") or []:
            if not isinstance(raw_word, dict):
                continue
            text = str(raw_word.get("text") or "").strip()
            if not text:
                continue
            try:
                begin_ms = int(raw_word["begin_time"])
                end_ms = int(raw_word["end_time"])
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(f"Word without usable timestamps in the sync response: {raw_word!r}") from None
            confidence = raw_word.get("confidence")
            try:
                probability = float(confidence) if confidence is not None else 0.0
            except (TypeError, ValueError):
                probability = 0.0
            words.append(
                {
                    "text": text,
                    "punctuation": str(raw_word.get("punctuation") or ""),
                    "begin_ms": begin_ms,
                    "end_ms": end_ms,
                    "probability": probability,
                }
            )

    if not texts and output.get("text"):
        texts.append(str(output["text"]).strip())

    usage = payload.get("usage") or {}
    try:
        reported = float(usage.get("duration") or 0)
    except (TypeError, ValueError):
        reported = 0.0
    return " ".join(text for text in texts if text), words, reported


def stitch_words(chunk_words: list[list[dict]], windows: list[ChunkWindow]) -> list[dict]:
    """Shift every chunk's word times by its start offset and concatenate in order."""
    if len(chunk_words) != len(windows):
        raise ValueError("chunk word lists and windows must align")
    stitched: list[dict] = []
    for words, window in zip(chunk_words, windows):
        offset_ms = int(round(window.start * 1000))
        for word in words:
            stitched.append(
                {
                    "word": word["text"],
                    "start": (word["begin_ms"] + offset_ms) / 1000,
                    "end": (word["end_ms"] + offset_ms) / 1000,
                    "probability": float(word.get("probability") or 0.0),
                }
            )
    return stitched


def validate_word_stream(
    words: list[dict],
    duration: float,
    *,
    coverage_tolerance: float = COVERAGE_TOLERANCE_SECONDS,
    overlap_tolerance: float = OVERLAP_TOLERANCE_SECONDS,
) -> None:
    """Fail when the stitched stream is not a trustworthy boundary source.

    The whole workflow treats these timestamps as the only boundary truth, so a
    stream that is unordered, overlapping, duplicated, or does not span the audio
    must stop the run here instead of corrupting every later stage.
    """
    if not words:
        raise RuntimeError("Transcription returned no words; refusing to continue without a word stream.")

    previous: dict | None = None
    for position, word in enumerate(words):
        if word["end"] < word["start"]:
            raise RuntimeError(f"Word {position + 1} ends before it starts: {word}")
        if previous is not None:
            if word["start"] < previous["start"]:
                raise RuntimeError(
                    f"Word timestamps are not monotonic at word {position + 1}: "
                    f"{word['start']:.3f}s after {previous['start']:.3f}s"
                )
            if word["start"] < previous["end"] - overlap_tolerance:
                raise RuntimeError(
                    f"Word timestamps overlap at word {position + 1}: "
                    f"{word['start']:.3f}s starts before {previous['end']:.3f}s ends"
                )
            if (
                word["start"] == previous["start"]
                and word["end"] == previous["end"]
                and word["word"] == previous["word"]
            ):
                raise RuntimeError(f"Duplicate word at position {position + 1}: {word}")
        previous = word

    first_start = words[0]["start"]
    last_end = words[-1]["end"]
    if first_start > coverage_tolerance:
        raise RuntimeError(
            f"Word stream starts at {first_start:.3f}s, missing the first {first_start:.1f}s of audio."
        )
    if last_end < duration - coverage_tolerance:
        raise RuntimeError(
            f"Word stream ends at {last_end:.3f}s but the audio is {duration:.3f}s; "
            f"the last {duration - last_end:.1f}s is uncovered."
        )


def transcribe_chunked_sync(
    *,
    media: Path,
    out_dir: Path,
    model: str,
    language: str,
    api_key: str,
    base_url: str,
    hotwords: dict[str, int] | None = None,
    max_chunk_seconds: float = DEFAULT_MAX_CHUNK_SECONDS,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    bitrate_kbps: int = DEFAULT_MP3_KBPS,
    poster,
    sleep=time.sleep,
) -> dict:
    """Transcribe `media` through the synchronous model and return the transcript.

    `poster(url, api_key, payload)` performs one HTTP POST and returns the parsed
    JSON response; it is injected so this function stays testable offline.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    normalized = normalize_audio(media, out_dir / "asr_sync_normalized.mp3", bitrate_kbps=bitrate_kbps)
    duration = probe_duration(normalized)
    silences = detect_silences(normalized)
    windows = plan_chunk_windows(duration, max_chunk_seconds, silences)
    print(
        f"Sync ASR plan: {duration:.1f}s audio in {len(windows)} chunk(s) "
        f"(limit {max_chunk_seconds:.0f}s, {bitrate_kbps}kbps mp3).",
        flush=True,
    )
    for window in windows:
        print(
            f"  chunk {window.index + 1}/{len(windows)}: {window.start:.2f}s - {window.end:.2f}s "
            f"({window.duration:.1f}s)",
            flush=True,
        )

    chunk_dir = out_dir / "asr_sync_chunks"
    chunk_paths: list[Path] = []
    if len(windows) == 1:
        chunk_paths.append(normalized)
    else:
        for window in windows:
            target = chunk_dir / f"chunk_{window.index + 1:03d}.mp3"
            chunk_paths.append(extract_chunk(normalized, window, target, bitrate_kbps=bitrate_kbps))

    url = f"{base_url}{SYNC_ENDPOINT_SUFFIX}"
    total = len(windows)

    def transcribe_one(index: int) -> tuple[str, list[dict]]:
        audio_bytes = chunk_paths[index].read_bytes()
        payload = build_sync_request(model=model, audio_bytes=audio_bytes, hotwords=hotwords)
        last_error: Exception | None = None
        for attempt in range(1, max(1, retries) + 1):
            try:
                response = poster(url, api_key, payload)
                text, words, _reported = parse_sync_response(response)
                if not words:
                    raise RuntimeError("response carried no words")
                return text, words
            except Exception as error:  # noqa: BLE001 - reported with the chunk index below
                last_error = error
                if attempt < max(1, retries):
                    sleep(min(2 ** (attempt - 1), 8))
        raise RuntimeError(
            f"Chunk {index + 1}/{total} failed after {max(1, retries)} attempt(s): {last_error}"
        )

    results: list[tuple[str, list[dict]] | None] = [None] * total
    workers = max(1, min(int(concurrency), total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(transcribe_one, index): index for index in range(total)}
        for future, index in futures.items():
            results[index] = future.result()

    texts: list[str] = []
    chunk_words: list[list[dict]] = []
    for item in results:
        if item is None:  # pragma: no cover - every future resolves or raises
            raise RuntimeError("A chunk produced no result.")
        texts.append(item[0])
        chunk_words.append(item[1])

    stitched = stitch_words(chunk_words, windows)
    validate_word_stream(stitched, duration)

    segments: list[dict] = []
    for index, (text, words) in enumerate(zip(texts, chunk_words)):
        window = windows[index]
        offset_ms = int(round(window.start * 1000))
        segment_words = [
            {
                "word": word["text"],
                "start": (word["begin_ms"] + offset_ms) / 1000,
                "end": (word["end_ms"] + offset_ms) / 1000,
                "probability": float(word.get("probability") or 0.0),
            }
            for word in words
        ]
        segments.append(
            {
                "id": index + 1,
                "start": window.start,
                "end": window.end,
                "text": text or " ".join(word["word"] for word in segment_words),
                "words": segment_words,
            }
        )

    return {
        "media": str(media),
        "provider": "aliyun-qwen-audio-asr-sync",
        "model": model,
        "language": language,
        "duration": duration,
        "segments": segments,
        "words": stitched,
    }
