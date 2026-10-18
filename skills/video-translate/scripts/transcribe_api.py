#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import (
    CREDENTIAL_KEYS,
    EnvFileSearch,
    describe_env_search,
    prepare_env_file,
    srt_time,
    write_json,
)
from media_probe import is_supported_audio, is_supported_video


# The fixed ASR stack: exactly one model. It accepts the whole recording as one
# asynchronous job, so long audio is never cut into pieces. Any other model name
# is refused rather than silently substituted, because word-level timestamps are
# the only boundary truth this workflow has.
ASR_MODEL_QWEN_AUDIO_31 = "qwen-audio-3.1-asr-flash-filetrans"
SUPPORTED_ASR_MODELS = (ASR_MODEL_QWEN_AUDIO_31,)
DEFAULT_ASR_MODEL = ASR_MODEL_QWEN_AUDIO_31
INLINE_HOTWORD_MODELS = frozenset({ASR_MODEL_QWEN_AUDIO_31})
MAX_ASR_HOTWORDS = 2000
MAX_ASR_SUPER_HOTWORDS = 50
ASR_SUPER_HOTWORD_WEIGHT = 50

PROVIDERS = {
    # Provider id kept stable for existing runs and CLI callers. The id is a
    # historical name; the only model behind it is the filetrans one above.
    "aliyun-fun-asr": {
        "key_env": "DASHSCOPE_API_KEY",
        "default_model": DEFAULT_ASR_MODEL,
        "supported_models": SUPPORTED_ASR_MODELS,
    },
}

RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$", re.IGNORECASE)
ALLOWED_ALIYUN_REGIONS = {"cn-beijing"}


def resolve_asr_model(requested: str | None, provider_cfg: dict) -> str:
    """Resolve the ASR model from ``--model``, then ``ALIYUN_ASR_MODEL``, then the default.

    ``ALIYUN_ASR_MODEL`` was documented in the ``.env`` template but never read, so
    every run silently used the provider default. Reading it here keeps the single
    supported model explicit and makes a stale value fail loudly.
    """
    supported = tuple(provider_cfg.get("supported_models") or ())
    candidate = (requested or os.environ.get("ALIYUN_ASR_MODEL") or provider_cfg["default_model"]).strip()
    if supported and candidate not in supported:
        raise RuntimeError(
            f"Unsupported ASR model {candidate!r}. Supported models: {', '.join(supported)}. "
            "The transcription stack is fixed; add a model deliberately only after revalidating "
            "its word-level timestamps against this workflow."
        )
    return candidate


def provider_label(model: str) -> str:
    """Report the transport and family that actually produced the transcript."""
    return "aliyun-qwen-audio-asr-filetrans"


def validate_hotword_text(text: str) -> str | None:
    """Return an error message when a hotword violates the provider's text rules."""
    if not text:
        return "hotword text is empty"
    if any(ord(character) > 127 for character in text):
        if len(text) > 15:
            return f"non-ASCII hotword exceeds 15 characters ({len(text)}): {text!r}"
        return None
    fragments = text.split()
    if len(fragments) > 7:
        return f"pure-ASCII hotword exceeds 7 space-separated fragments ({len(fragments)}): {text!r}"
    return None


def normalize_hotwords(entries: list[dict] | dict[str, int]) -> list[dict]:
    """Validate and deduplicate hotwords so a request stays reproducible.

    Accepts the precompiled list form (``[{"text": ..., "weight": ...}]``) or the
    inline map form (``{"text": weight}``) and returns the list form. Invalid input
    fails loudly instead of silently dropping entries.
    """
    if isinstance(entries, dict):
        raw_entries: list[dict] = [{"text": text, "weight": weight} for text, weight in entries.items()]
    else:
        raw_entries = list(entries)

    normalized: list[dict] = []
    position_by_key: dict[str, int] = {}
    super_count = 0
    for entry in raw_entries:
        text = str(entry.get("text") or "").strip()
        problem = validate_hotword_text(text)
        if problem:
            raise ValueError(problem)
        key = text.casefold()
        weight_raw = entry.get("weight", 4)
        try:
            weight = int(weight_raw)
        except (TypeError, ValueError):
            raise ValueError(f"hotword weight must be an integer: {text!r} -> {weight_raw!r}") from None
        if weight != ASR_SUPER_HOTWORD_WEIGHT and not 1 <= weight <= 5:
            raise ValueError(
                f"hotword weight must be in [1, 5] or {ASR_SUPER_HOTWORD_WEIGHT}: {text!r} -> {weight}"
            )
        existing = position_by_key.get(key)
        if existing is not None:
            # A case-insensitive duplicate keeps the first spelling but never
            # silently drops the stronger weight the caller asked for.
            if weight > normalized[existing]["weight"]:
                if normalized[existing]["weight"] == ASR_SUPER_HOTWORD_WEIGHT:
                    super_count -= 1
                if weight == ASR_SUPER_HOTWORD_WEIGHT:
                    super_count += 1
                normalized[existing]["weight"] = weight
            continue
        if weight == ASR_SUPER_HOTWORD_WEIGHT:
            super_count += 1
        item = {"text": text, "weight": weight}
        lang = str(entry.get("lang") or "").strip()
        if lang:
            item["lang"] = lang
        position_by_key[key] = len(normalized)
        normalized.append(item)

    if super_count > MAX_ASR_SUPER_HOTWORDS:
        raise ValueError(
            f"at most {MAX_ASR_SUPER_HOTWORDS} super hotwords (weight {ASR_SUPER_HOTWORD_WEIGHT}) "
            f"are allowed; got {super_count}"
        )
    if len(normalized) > MAX_ASR_HOTWORDS:
        raise ValueError(f"at most {MAX_ASR_HOTWORDS} hotwords are allowed; got {len(normalized)}")
    return normalized


def inline_hotwords_map(entries: list[dict]) -> dict[str, int]:
    return {str(entry["text"]): int(entry["weight"]) for entry in entries}


def load_hotwords_file(path: Path) -> dict[str, int]:
    """Load a local hotword file as an inline ``parameters.vocabulary`` map."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "vocabulary" in data:
        data = data["vocabulary"]
    if not isinstance(data, (dict, list)):
        raise ValueError(f"Hotword file {path} must contain a JSON object or array.")
    return inline_hotwords_map(normalize_hotwords(data))



def api_retry_attempts() -> int:
    raw = os.environ.get("VIDEO_TRANSLATE_API_RETRIES") or "3"
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def retry_delay(attempt: int) -> float:
    return min(30.0, 2.0 * (2 ** (attempt - 1)))


def request_bytes(request: Request, *, timeout: float, label: str) -> bytes:
    attempts = api_retry_attempts()
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in RETRYABLE_HTTP_CODES
            if not retryable or attempt >= attempts:
                raise RuntimeError(f"{label} returned HTTP {exc.code}: {detail}") from exc
            wait = retry_delay(attempt)
            print(f"{label} returned HTTP {exc.code}; retrying in {wait:.0f}s ({attempt}/{attempts})...", flush=True)
            time.sleep(wait)
        except (URLError, TimeoutError, socket.timeout) as exc:
            if attempt >= attempts:
                raise RuntimeError(f"{label} failed after {attempts} attempts: {exc}") from exc
            wait = retry_delay(attempt)
            print(f"{label} failed: {exc}; retrying in {wait:.0f}s ({attempt}/{attempts})...", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"{label} failed unexpectedly.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Internal Alibaba ASR transcription helper with word timestamps.")
    parser.add_argument("media", type=Path, help="Input video, or workflow-prepared audio.")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="aliyun-fun-asr")
    parser.add_argument("--model", default=None, help="Provider model. Defaults to provider-specific choice.")
    parser.add_argument("--language", default="en", help="Source language hint such as en, fr, es, it.")
    parser.add_argument("--out-dir", type=Path, default=Path("runs/api-transcript"))
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Explicit local .env path. Omit to discover one: $ALIYUN_ENV_FILE, then the nearest "
            ".env at or above the Skill directory and the current directory that declares a "
            "provider credential key."
        ),
    )
    parser.add_argument("--workspace-id", default=None, help="Alibaba Model Studio workspace ID.")
    parser.add_argument("--region", default=None, help="Alibaba region, for example cn-beijing.")
    parser.add_argument("--vocabulary-id", default=None, help="Precompiled Alibaba hotword vocabulary ID.")
    parser.add_argument(
        "--hotwords-file",
        default=None,
        help=(
            "Local JSON hotword file sent as inline ASR hotwords. Accepts a {text: weight} object, "
            "a list of {text, weight, lang} objects, or an object with a 'vocabulary' array."
        ),
    )
    parser.add_argument(
        "--confirm-external-processing",
        action="store_true",
        help="Required acknowledgement before the selected audio is uploaded to Model Studio temporary storage and submitted to the Alibaba ASR service.",
    )
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between async task polls.")
    parser.add_argument("--timeout", type=float, default=7200.0, help="Async task timeout in seconds.")
    parser.add_argument("--keep-audio", action="store_true", help="Keep extracted API upload audio in out-dir.")
    parser.add_argument("--workflow-video", type=Path, default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


def prepare_env(explicit: Path | None) -> EnvFileSearch:
    """Resolve and load the local env file, reporting exactly which one was used.

    Anchored on the Skill directory rather than the caller's working directory,
    because this runs with the Skill directory as cwd while the credentials may
    live in a workspace root above it. A caller-named path that does not exist is
    reported so the caller stops, instead of continuing without credentials and
    later failing in a way that looks like a provider outage.
    """
    return prepare_env_file(explicit, start=Path(__file__).resolve().parent)


def required_credentials(model: str | None = None) -> tuple[str, ...]:
    """Credentials this fixed stack needs. Every model runs over the same path."""
    return CREDENTIAL_KEYS


def missing_credentials(model: str | None = None) -> list[str]:
    return [key for key in required_credentials(model) if not has_real_secret(os.environ.get(key))]


def has_real_secret(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return stripped != "..." and not stripped.startswith("填写")


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for audio extraction but was not found on PATH.")


def upload_bitrate_kbps() -> int:
    """Uploaded audio quality; the file must stay small but keep speech intact."""
    raw = (os.environ.get("ALIYUN_ASR_MP3_KBPS") or "").strip()
    if not raw:
        return 64
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"ALIYUN_ASR_MP3_KBPS must be an integer, got {raw!r}") from None
    if not 16 <= value <= 96:
        raise RuntimeError("ALIYUN_ASR_MP3_KBPS must be between 16 and 96.")
    return value


def extract_audio(media: Path, out_dir: Path) -> Path:
    if is_supported_audio(media):
        print(f"Using validated workflow audio without ffmpeg extraction: {media}", flush=True)
        return media
    require_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    audio_path = out_dir / "api_upload_audio.mp3"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(media),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-b:a",
        f"{upload_bitrate_kbps()}k",
        str(audio_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return audio_path


def should_delete_prepared_audio(media: Path, audio_path: Path, keep_audio: bool) -> bool:
    """Delete only audio generated by this script, never caller-supplied audio."""
    return not keep_audio and audio_path.resolve() != media.resolve()


def validate_workflow_audio(audio: Path, workflow_video: Path | None) -> Path:
    """Bind internal audio reuse to one unambiguous single-video project."""
    resolved_audio = audio.expanduser().resolve()
    if workflow_video is None:
        raise RuntimeError("Direct audio input is not supported; workflow audio must be bound to its video.")
    resolved_video = workflow_video.expanduser().resolve()
    if not resolved_video.is_file() or not is_supported_video(resolved_video):
        raise RuntimeError("Workflow audio must reference an existing supported video file.")
    allowed_parents = {
        resolved_video.parent,
        (resolved_video.parent / ".work" / "input").resolve(),
    }
    if resolved_audio.parent not in allowed_parents:
        raise RuntimeError("Workflow audio must be beside the video or under its .work/input directory.")
    if resolved_audio.stem != resolved_video.stem:
        project_videos = [
            child.resolve()
            for child in resolved_video.parent.iterdir()
            if child.is_file() and is_supported_video(child)
        ]
        project_audio = [
            child.resolve()
            for child in resolved_audio.parent.iterdir()
            if child.is_file() and is_supported_audio(child)
        ]
        if project_videos != [resolved_video] or project_audio != [resolved_audio]:
            raise RuntimeError(
                "A different-basename workflow audio file is allowed only when it is the sole audio "
                "inside a dedicated single-video project."
            )
    return resolved_audio


def multipart_body(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = f"----codex-subtitle-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode())
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        chunks.append(str(value).encode())
        chunks.append(b"\r\n")

    chunks.append(f"--{boundary}\r\n".encode())
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
    )
    chunks.append(file_path.read_bytes())
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def http_json_request(url: str, api_key: str, *, method: str = "GET", payload: dict | None = None, headers: dict | None = None) -> dict:
    body = None
    request_headers = {"Authorization": f"Bearer {api_key}"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)

    request = Request(url, data=body, headers=request_headers, method=method)
    data = request_bytes(request, timeout=120, label=f"{method} {url}")
    return json.loads(data.decode("utf-8"))


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 120,
) -> dict:
    request_headers = dict(headers or {})
    body = data
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = Request(url, data=body, headers=request_headers, method=method)
    raw = request_bytes(request, timeout=timeout, label=f"{method} {url}")
    return json.loads(raw.decode("utf-8"))


def require_https_url(url: str, label: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"{label} must be an HTTPS URL.")


def validated_aliyun_base_url(workspace_id: str, region: str) -> str:
    if not IDENTIFIER_RE.fullmatch(workspace_id):
        raise RuntimeError("ALIYUN_WORKSPACE_ID must contain only letters, digits, and hyphens.")
    if region not in ALLOWED_ALIYUN_REGIONS:
        raise RuntimeError("ALIYUN_REGION must be cn-beijing for this fixed production workflow.")
    return f"https://{workspace_id}.{region}.maas.aliyuncs.com"


def aliyun_base_url(workspace_id: str, region: str) -> str:
    return validated_aliyun_base_url(workspace_id, region)


DASHSCOPE_UPLOAD_URL = "https://dashscope.aliyuncs.com/api/v1/uploads"


def fetch_upload_policy(api_key: str, model: str) -> dict:
    """Fetch Model Studio's free temporary-storage upload policy for one model.

    The temporary space removes the need for any third-party file host: the audio
    is uploaded straight into Alibaba's own staging area and referenced by an
    `oss://` URL that the transcription service resolves internally.
    """
    url = f"{DASHSCOPE_UPLOAD_URL}?action=getPolicy&model={quote(model)}"
    response = http_json_request(url, api_key)
    data = response.get("data")
    if not isinstance(data, dict) or not data.get("upload_host"):
        raise RuntimeError(f"Model Studio did not return an upload policy: {response}")
    require_https_url(str(data["upload_host"]), "Model Studio upload host")
    return data


UPLOAD_OBJECT_STEM = "asr_upload"


def upload_object_name(source: Path, *, token: str | None = None) -> str:
    """A transport-safe object name for the temporary upload.

    The object name becomes part of an ``oss://`` URL, so a ``#`` in the caller's
    filename turns the rest of the path into a URL fragment and the service looks
    for a different object (HTTP 400 BadRequest.ResourceNotExist). The upload never
    needs the original name, so it is replaced with a neutral one that keeps only
    the extension the service uses to detect the audio format.
    """
    suffix = source.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        suffix = ".mp3"
    return f"{UPLOAD_OBJECT_STEM}_{token or uuid.uuid4().hex[:8]}{suffix}"


def upload_audio_to_temporary_storage(audio_path: Path, api_key: str, model: str) -> str:
    """Upload the audio into Model Studio's temporary storage and return its oss:// URL."""
    policy = fetch_upload_policy(api_key, model)
    size_mb = audio_path.stat().st_size / 1024 / 1024
    max_mb = policy.get("max_file_size_mb")
    if max_mb:
        try:
            limit = float(max_mb)
        except (TypeError, ValueError):
            limit = 0.0
        if limit and size_mb > limit:
            raise RuntimeError(
                f"Audio is {size_mb:.1f} MB but the temporary upload accepts {limit:.0f} MB; "
                "lower ALIYUN_ASR_MP3_KBPS or shorten the input."
            )
    object_name = upload_object_name(audio_path)
    key = f"{policy.get('upload_dir') or ''}/{object_name}".lstrip("/")
    fields = {
        "OSSAccessKeyId": str(policy["oss_access_key_id"]),
        "Signature": str(policy["signature"]),
        "policy": str(policy["policy"]),
        "x-oss-object-acl": str(policy.get("x_oss_object_acl") or "private"),
        "x-oss-forbid-overwrite": str(policy.get("x_oss_forbid_overwrite") or "true"),
        "key": key,
        "success_action_status": "200",
    }
    body, boundary = multipart_body(fields, "file", audio_path)
    request = Request(
        str(policy["upload_host"]),
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
        method="POST",
    )
    request_bytes(request, timeout=600, label="Model Studio temporary upload")
    print(
        f"Uploaded {size_mb:.1f} MB as {object_name} to Model Studio temporary storage (valid 48 h, no OSS bucket).",
        flush=True,
    )
    return f"oss://{key}"


def submit_aliyun_fun_asr(
    *,
    api_key: str,
    workspace_id: str,
    region: str,
    model: str,
    file_url: str,
    language: str,
    vocabulary_id: str | None,
    hotwords: dict[str, int] | None = None,
) -> dict:
    # Qwen-Audio-3.x-ASR-Flash-Filetrans requires `parameters` to be present and
    # fails the submitted task when it is omitted, so this dict is always sent.
    parameters: dict[str, object] = {
        "channel_id": [0],
        "diarization_enabled": False,
        "language_hints": [language],
    }
    if vocabulary_id:
        parameters["vocabulary_id"] = vocabulary_id
    if hotwords:
        if model not in INLINE_HOTWORD_MODELS:
            raise RuntimeError(
                f"Inline hotwords are not supported by {model}. Use a precompiled vocabulary_id whose "
                f"target_model is {model!r}, or select one of: {', '.join(sorted(INLINE_HOTWORD_MODELS))}."
            )
        parameters["vocabulary"] = dict(hotwords)

    payload = {
        "model": model,
        "input": {"file_urls": [file_url]},
        "parameters": parameters,
    }
    headers = {"X-DashScope-Async": "enable"}
    if file_url.startswith("oss://"):
        # Model Studio requires this header to resolve an oss:// temporary URL.
        headers["X-DashScope-OssResourceResolve"] = "enable"
    return http_json_request(
        f"{aliyun_base_url(workspace_id, region)}/api/v1/services/audio/asr/transcription",
        api_key,
        method="POST",
        payload=payload,
        headers=headers,
    )


def poll_aliyun_task(
    *,
    api_key: str,
    workspace_id: str,
    region: str,
    task_id: str,
    poll_interval: float,
    timeout: float,
) -> dict:
    started_at = time.monotonic()
    last_heartbeat = started_at
    deadline = started_at + timeout
    last_status = ""
    task_url = f"{aliyun_base_url(workspace_id, region)}/api/v1/tasks/{task_id}"

    while True:
        task = http_json_request(task_url, api_key)
        output = task.get("output") or {}
        status = str(output.get("task_status") or "").upper()
        now = time.monotonic()
        if status != last_status:
            print(f"Aliyun task {task_id}: {status or 'UNKNOWN'}", flush=True)
            last_status = status
            last_heartbeat = now
        elif now - last_heartbeat >= 300:
            elapsed = (now - started_at) / 60
            print(f"Aliyun task {task_id}: still {status or 'UNKNOWN'} after {elapsed:.1f} min", flush=True)
            last_heartbeat = now

        if status in {"SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"}:
            return task
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out waiting for Aliyun task {task_id}. Last status: {status}")
        time.sleep(poll_interval)


def download_json(url: str) -> dict:
    raw = request_bytes(Request(url), timeout=300, label=f"Download {url}")
    return json.loads(raw.decode("utf-8"))


CONTRACTION_SUFFIXES = {"m", "re", "ve", "ll", "d", "s", "t"}


def word_confidence(raw_word: dict) -> float:
    """Read a word's confidence from the provider response.

    The transcription result carries `confidence` per word. It was previously
    discarded and hardcoded to 0.0, which threw away data the word table is
    supposed to preserve. An absent value stays 0.0 so the field keeps its type.
    """
    raw = raw_word.get("confidence")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def normalize_aliyun_sentence_words(raw_words: list[dict]) -> list[dict]:
    words: list[dict] = []
    pending_apostrophe: dict | None = None

    for raw_word in raw_words:
        text = str(raw_word.get("text") or "").strip()
        if not text:
            continue
        start = float(raw_word["begin_time"]) / 1000
        end = float(raw_word["end_time"]) / 1000
        confidence = word_confidence(raw_word)

        if text in {"'", "’"}:
            pending_apostrophe = {"start": start, "end": end}
            continue

        if pending_apostrophe and words and text.lower() in CONTRACTION_SUFFIXES:
            previous = words[-1]
            previous["word"] = f"{previous['word']}'{text}"
            previous["end"] = end
            pending_apostrophe = None
            continue

        pending_apostrophe = None
        words.append(
            {
                "word": text,
                "start": start,
                "end": end,
                "probability": confidence,
            }
        )

    return words


def normalize_aliyun_response(media: Path, model: str, language: str, raw: dict) -> dict:
    words: list[dict] = []
    segments: list[dict] = []

    for transcript in raw.get("transcripts") or []:
        for sentence in transcript.get("sentences") or []:
            segment_words = normalize_aliyun_sentence_words(sentence.get("words") or [])
            words.extend(segment_words)

            start = float(sentence.get("begin_time", segment_words[0]["start"] * 1000 if segment_words else 0)) / 1000
            end = float(sentence.get("end_time", segment_words[-1]["end"] * 1000 if segment_words else 0)) / 1000
            text = str(sentence.get("text") or " ".join(word["word"] for word in segment_words)).strip()
            segments.append(
                {
                    "id": len(segments) + 1,
                    "start": start,
                    "end": end,
                    "text": text,
                    "words": segment_words,
                }
            )

    # Word-level timestamps are the only boundary truth for this workflow. A model
    # that returned sentence-level text only must fail here, loudly, instead of
    # producing a transcript whose segments have no word stream to align against.
    if not words:
        raise RuntimeError(
            f"ASR model {model!r} returned no word-level timestamps. This workflow requires "
            "sentence-level words[] with begin_time/end_time; transcription is unusable without them."
        )

    if not segments:
        segments = build_segments_from_words(words)

    properties = raw.get("properties") or {}
    duration = float(properties.get("original_duration_in_milliseconds") or 0) / 1000
    if not duration and words:
        duration = float(words[-1]["end"])

    return {
        "media": str(media),
        "provider": provider_label(model),
        "model": model,
        "language": language,
        "duration": duration,
        "segments": segments,
        "words": words,
    }


def transcribe_aliyun_fun_asr(args: argparse.Namespace, api_key: str, model: str) -> int:
    workspace_id = args.workspace_id or os.environ.get("ALIYUN_WORKSPACE_ID")
    region = args.region or os.environ.get("ALIYUN_REGION") or "cn-beijing"
    vocabulary_id = args.vocabulary_id or os.environ.get("ALIYUN_ASR_VOCABULARY_ID") or None

    hotwords: dict[str, int] | None = None
    hotwords_file = args.hotwords_file or os.environ.get("ALIYUN_ASR_HOTWORDS_FILE") or None
    if hotwords_file:
        hotwords = load_hotwords_file(Path(hotwords_file))
        print(f"Loaded {len(hotwords)} inline ASR hotwords from {hotwords_file}", flush=True)
    if vocabulary_id and model in INLINE_HOTWORD_MODELS:
        print(
            f"NOTE: vocabulary_id={vocabulary_id} only applies when it was created with "
            f"target_model={model!r}. A list created for a different model is accepted silently and "
            "its hotwords do NOT apply; verify it with "
            "tools/migrate_asr_hotwords.py --source-vocabulary-id before trusting the result.",
            flush=True,
        )

    if not workspace_id:
        print("Missing Alibaba workspace ID. Set ALIYUN_WORKSPACE_ID in .env or pass --workspace-id.", file=sys.stderr)
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    audio_path: Path | None = None
    print("Preparing selected audio for transcription...", flush=True)
    audio_path = extract_audio(args.media, args.out_dir)
    print(f"Audio upload file: {audio_path} ({audio_path.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    args.file_url = upload_audio_to_temporary_storage(audio_path, api_key, model)

    submit_path = args.out_dir / "aliyun_task_submit.json"
    result_path = args.out_dir / "aliyun_task_result.json"
    if result_path.exists():
        try:
            previous_task = json.loads(result_path.read_text(encoding="utf-8"))
            previous_status = str((previous_task.get("output") or {}).get("task_status") or "").upper()
            if previous_status in {"FAILED", "CANCELED", "UNKNOWN"}:
                print(f"Previous Aliyun task ended as {previous_status}; submitting a new task.", flush=True)
                submit_path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"Ignoring unreadable Aliyun task cache: {exc}", flush=True)

    if submit_path.exists():
        submit = json.loads(submit_path.read_text(encoding="utf-8"))
        print(f"Using existing Aliyun task submission: {submit_path}", flush=True)
    else:
        print(f"Submitting Aliyun filetrans task model={model} region={region}...", flush=True)
        submit = submit_aliyun_fun_asr(
            api_key=api_key,
            workspace_id=workspace_id,
            region=region,
            model=model,
            file_url=args.file_url,
            language=args.language,
            vocabulary_id=vocabulary_id,
            hotwords=hotwords,
        )
        write_json(submit_path, submit)
    task_id = str((submit.get("output") or {}).get("task_id") or "")
    if not task_id:
        raise RuntimeError(f"Aliyun submit response did not include task_id: {submit}")

    task = poll_aliyun_task(
        api_key=api_key,
        workspace_id=workspace_id,
        region=region,
        task_id=task_id,
        poll_interval=args.poll_interval,
        timeout=args.timeout,
    )
    write_json(args.out_dir / "aliyun_task_result.json", task)

    output = task.get("output") or {}
    if str(output.get("task_status") or "").upper() != "SUCCEEDED":
        raise RuntimeError(f"Aliyun task did not succeed: {task}")

    results = output.get("results") or []
    succeeded = [item for item in results if str(item.get("subtask_status") or "").upper() == "SUCCEEDED"]
    if not succeeded:
        raise RuntimeError(f"Aliyun task had no successful subtasks: {task}")

    transcription_url = str(succeeded[0].get("transcription_url") or "")
    if not transcription_url:
        raise RuntimeError(f"Aliyun successful subtask did not include transcription_url: {succeeded[0]}")

    print("Downloading Aliyun transcription JSON...", flush=True)
    raw = download_json(transcription_url)
    write_json(args.out_dir / "aliyun_transcription_raw.json", raw)
    write_json(args.out_dir / "api_raw_response.json", raw)

    transcript = normalize_aliyun_response(args.media, model, args.language, raw)
    write_json(args.out_dir / "transcript_words.json", transcript)
    write_srt(args.out_dir / "transcript_raw.srt", transcript["segments"])
    if audio_path and should_delete_prepared_audio(args.media, audio_path, args.keep_audio):
        audio_path.unlink(missing_ok=True)

    print(f"Wrote {args.out_dir / 'transcript_words.json'}")
    print(f"Wrote {args.out_dir / 'transcript_raw.srt'}")
    print(f"Words: {len(transcript['words'])}; segments: {len(transcript['segments'])}")
    return 0


def build_segments_from_words(words: list[dict], max_words: int = 18, max_gap: float = 0.8) -> list[dict]:
    segments: list[dict] = []
    current: list[dict] = []

    def flush() -> None:
        if not current:
            return
        segments.append(
            {
                "id": len(segments) + 1,
                "start": current[0]["start"],
                "end": current[-1]["end"],
                "text": " ".join(word["word"] for word in current),
                "words": list(current),
            }
        )
        current.clear()

    previous_end: float | None = None
    for word in words:
        gap = 0 if previous_end is None else word["start"] - previous_end
        if current and (len(current) >= max_words or gap > max_gap):
            flush()
        current.append(word)
        previous_end = word["end"]
    flush()
    return segments


def write_srt(path: Path, segments: list[dict]) -> None:
    blocks = []
    for i, segment in enumerate(segments, start=1):
        text = segment.get("text") or " ".join(word["word"] for word in segment.get("words", []))
        blocks.append(f"{i}\n{srt_time(segment['start'])} --> {srt_time(segment['end'])}\n{text.strip()}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    # Load the local env file before resolving the model, so ALIYUN_ASR_MODEL,
    # ALIYUN_ASR_VOCABULARY_ID, and ALIYUN_ASR_HOTWORDS_FILE from `.env` apply.
    env_search = prepare_env(args.env_file)
    if env_search.explicit and env_search.path is None:
        return 2
    provider_cfg = PROVIDERS[args.provider]
    try:
        model = resolve_asr_model(args.model, provider_cfg)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if is_supported_audio(args.media):
        try:
            validate_workflow_audio(args.media, args.workflow_video)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if not args.confirm_external_processing:
        print(
            "Refusing external processing without --confirm-external-processing. "
            "This uploads the selected audio to Model Studio temporary storage (48-hour retention) and sends it to the Alibaba ASR service.",
            file=sys.stderr,
        )
        return 2
    # Name every missing credential at once, with the paths that were searched, so
    # a configuration problem is never misread as a provider outage. The provider
    # key is covered here rather than by a separate earlier check, which would
    # return before the complete diagnosis could be printed.
    missing = missing_credentials(model)
    if missing:
        print(f"Missing provider credentials: {', '.join(missing)}.", file=sys.stderr)
        print(describe_env_search(env_search), file=sys.stderr)
        return 2
    api_key = os.environ.get(provider_cfg["key_env"])

    result = transcribe_aliyun_fun_asr(args, api_key, model)
    elapsed = time.monotonic() - started_at
    print(f"Done in {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
