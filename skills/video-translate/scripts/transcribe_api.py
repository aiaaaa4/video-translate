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
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from common import srt_time, write_json
from media_probe import is_supported_audio, is_supported_video


ASR_MODEL_QWEN_AUDIO_31 = "qwen-audio-3.1-asr-flash-filetrans"
ASR_MODEL_QWEN_AUDIO_30 = "qwen-audio-3.0-asr-flash-filetrans"
ASR_MODEL_FUN_ASR = "fun-asr"
# The fixed ASR stack. Every model here must return sentence-level `words[]` with
# word timestamps, because those timestamps are the only subtitle boundary truth.
SUPPORTED_ASR_MODELS = (ASR_MODEL_QWEN_AUDIO_31, ASR_MODEL_QWEN_AUDIO_30, ASR_MODEL_FUN_ASR)
# Inline ("instant") hotwords are accepted only by the Qwen-Audio-3.x-ASR-Flash family.
INLINE_HOTWORD_MODELS = frozenset({ASR_MODEL_QWEN_AUDIO_31, ASR_MODEL_QWEN_AUDIO_30})
MAX_ASR_HOTWORDS = 2000
MAX_ASR_SUPER_HOTWORDS = 50
ASR_SUPER_HOTWORD_WEIGHT = 50

PROVIDERS = {
    # Provider id kept stable for existing runs and CLI callers; the family now
    # covers the Qwen-Audio-3.x-ASR-Flash-Filetrans and Fun-ASR model families,
    # which share one HTTP endpoint and one result schema.
    "aliyun-fun-asr": {
        "key_env": "DASHSCOPE_API_KEY",
        "default_model": ASR_MODEL_QWEN_AUDIO_31,
        "supported_models": SUPPORTED_ASR_MODELS,
    },
}

RETRYABLE_HTTP_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
OKFILE_UPLOAD_URL = "https://www.okfile.com/api/upload/quick"
OKFILE_ORIGIN = "https://www.okfile.com"
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$", re.IGNORECASE)
ALLOWED_ALIYUN_REGIONS = {"cn-beijing"}


def resolve_asr_model(requested: str | None, provider_cfg: dict) -> str:
    """Resolve the ASR model from ``--model``, then ``ALIYUN_ASR_MODEL``, then the default.

    ``ALIYUN_ASR_MODEL`` was documented in the ``.env`` template but never read, so
    every run silently used the provider default. Reading it here makes the fixed
    stack pinnable and keeps ``fun-asr`` available as a rollback.
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
    """Report the ASR family that actually produced the transcript."""
    return "aliyun-qwen-audio-asr" if model in INLINE_HOTWORD_MODELS else "aliyun-fun-asr"


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
    parser = argparse.ArgumentParser(description="Internal Fun-ASR transcription helper with word timestamps.")
    parser.add_argument("media", type=Path, help="Input video, or workflow-prepared audio.")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="aliyun-fun-asr")
    parser.add_argument("--model", default=None, help="Provider model. Defaults to provider-specific choice.")
    parser.add_argument("--language", default="en", help="Source language hint such as en, fr, es, it.")
    parser.add_argument("--out-dir", type=Path, default=Path("runs/api-transcript"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
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
        help="Required acknowledgement before the selected audio is uploaded to OkFile and submitted to Alibaba Fun-ASR.",
    )
    parser.add_argument("--poll-interval", type=float, default=10.0, help="Seconds between async task polls.")
    parser.add_argument("--timeout", type=float, default=7200.0, help="Async task timeout in seconds.")
    parser.add_argument("--keep-audio", action="store_true", help="Keep extracted API upload audio in out-dir.")
    parser.add_argument("--workflow-video", type=Path, default=None, help=argparse.SUPPRESS)
    return parser.parse_args()


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def has_real_secret(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return stripped != "..." and not stripped.startswith("填写")


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for audio extraction but was not found on PATH.")


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
        "64k",
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


def okfile_token() -> str | None:
    return os.environ.get("OKFILE_TOKEN")


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


def okfile_auth_headers(token: str) -> dict[str, str]:
    return {
        "X-API-Key": token,
        "User-Agent": "codex-video-subtitle-workflow/0.1",
    }


def okfile_public_url(response: dict) -> str:
    public_url = str(response.get("downloadUrl") or response.get("url") or "")
    if not public_url:
        raise RuntimeError(f"OkFile response did not include downloadUrl or url: {response}")
    require_https_url(public_url, "OkFile public URL")
    return public_url


def okfile_upload_quick(audio_path: Path, upload_url: str, token: str) -> dict:
    body, boundary = multipart_body({}, "file", audio_path)
    headers = okfile_auth_headers(token)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    return request_json(upload_url, method="POST", data=body, headers=headers, timeout=600)


def put_file(url: str, data: bytes, content_type: str) -> None:
    require_https_url(url, "OkFile signed upload URL")
    request = Request(
        url,
        data=data,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(len(data)),
            "User-Agent": "codex-video-subtitle-workflow/0.1",
        },
        method="PUT",
    )
    request_bytes(request, timeout=600, label="OkFile signed PUT upload")


def okfile_upload_standard(audio_path: Path, upload_url: str, token: str, config: dict) -> dict:
    content_type = mimetypes.guess_type(audio_path.name)[0] or "application/octet-stream"
    preferred_part_size = int(config.get("partSize") or 10 * 1024 * 1024)
    payload = {
        "filename": audio_path.name,
        "size": audio_path.stat().st_size,
        "contentType": content_type,
        "preferredPartSize": preferred_part_size,
    }
    prepare = request_json(
        f"{OKFILE_ORIGIN}/api/upload/prepare",
        method="POST",
        payload=payload,
        headers=okfile_auth_headers(token),
    )
    if not prepare.get("success"):
        raise RuntimeError(f"OkFile prepare failed: {prepare}")

    mode = str(prepare.get("mode") or "")
    if mode == "single":
        put_file(str(prepare["uploadUrl"]), audio_path.read_bytes(), content_type)
    elif mode == "multipart":
        part_size = int(prepare.get("partSize") or preferred_part_size)
        with audio_path.open("rb") as file_obj:
            for part in prepare.get("parts") or []:
                part_number = int(part["partNumber"])
                file_obj.seek((part_number - 1) * part_size)
                chunk = file_obj.read(part_size)
                print(f"Uploading OkFile part {part_number}/{prepare.get('totalParts')}...", flush=True)
                put_file(str(part["uploadUrl"]), chunk, content_type)
    else:
        raise RuntimeError(f"Unknown OkFile upload mode: {prepare}")

    complete = request_json(
        f"{OKFILE_ORIGIN}/api/upload/complete",
        method="POST",
        payload={"id": prepare["id"]},
        headers=okfile_auth_headers(token),
    )
    if complete.get("missingParts"):
        missing = ", ".join(str(part) for part in complete["missingParts"])
        raise RuntimeError(f"OkFile upload is incomplete. Missing parts: {missing}")
    if not complete.get("success"):
        raise RuntimeError(f"OkFile complete failed: {complete}")
    return complete


def upload_audio_to_okfile(audio_path: Path, out_dir: Path) -> str:
    token = okfile_token()
    if not has_real_secret(token):
        raise RuntimeError("Missing OKFILE_TOKEN in .env. Create an OkFile API key and store the full token locally.")

    upload_url = OKFILE_UPLOAD_URL

    cached_response = out_dir / "okfile_upload_response.json"
    if cached_response.exists():
        age_seconds = time.time() - cached_response.stat().st_mtime
        if age_seconds < 20 * 3600:
            try:
                public_url = okfile_public_url(json.loads(cached_response.read_text(encoding="utf-8")))
                print(f"Using existing OkFile public URL from {cached_response}", flush=True)
                return public_url
            except Exception as exc:
                print(f"Ignoring stale/invalid OkFile upload cache: {exc}", flush=True)

    config = request_json(f"{OKFILE_ORIGIN}/api/upload/config", headers=okfile_auth_headers(str(token)))
    write_json(out_dir / "okfile_upload_config.json", config)

    size = audio_path.stat().st_size
    quick_limit = int(config.get("quickUploadMaxSize") or 0)
    if quick_limit and size <= quick_limit:
        print(f"Uploading audio to OkFile quick endpoint ({size / 1024 / 1024:.1f} MB)...", flush=True)
        response = okfile_upload_quick(audio_path, upload_url, str(token))
    else:
        print(f"Uploading audio to OkFile standard flow ({size / 1024 / 1024:.1f} MB)...", flush=True)
        response = okfile_upload_standard(audio_path, upload_url, str(token), config)

    write_json(out_dir / "okfile_upload_response.json", response)
    public_url = okfile_public_url(response)
    print(f"OkFile public URL: {public_url}", flush=True)
    return public_url


def aliyun_base_url(workspace_id: str, region: str) -> str:
    return validated_aliyun_base_url(workspace_id, region)


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
    return http_json_request(
        f"{aliyun_base_url(workspace_id, region)}/api/v1/services/audio/asr/transcription",
        api_key,
        method="POST",
        payload=payload,
        headers={"X-DashScope-Async": "enable"},
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


def normalize_aliyun_sentence_words(raw_words: list[dict]) -> list[dict]:
    words: list[dict] = []
    pending_apostrophe: dict | None = None

    for raw_word in raw_words:
        text = str(raw_word.get("text") or "").strip()
        if not text:
            continue
        start = float(raw_word["begin_time"]) / 1000
        end = float(raw_word["end_time"]) / 1000

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
                "probability": 0.0,
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
    print("Preparing selected audio for OkFile upload...", flush=True)
    audio_path = extract_audio(args.media, args.out_dir)
    print(f"Audio upload file: {audio_path} ({audio_path.stat().st_size / 1024 / 1024:.1f} MB)", flush=True)
    args.file_url = upload_audio_to_okfile(audio_path, args.out_dir)

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
        print(f"Submitting Aliyun Fun-ASR task model={model} region={region}...", flush=True)
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
    load_env(args.env_file)
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
            "This uploads the selected audio to OkFile and sends its temporary URL to Alibaba ASR.",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get(provider_cfg["key_env"])
    if not has_real_secret(api_key):
        print(
            f"Missing API key. Set {provider_cfg['key_env']} in {args.env_file} or your environment.",
            file=sys.stderr,
        )
        return 2

    result = transcribe_aliyun_fun_asr(args, api_key, model)
    elapsed = time.monotonic() - started_at
    print(f"Done in {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
