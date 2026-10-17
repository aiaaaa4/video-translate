#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from common import (
    normalize_word,
    prepare_env_file,
    read_env_file,
)
from source_subtitle_reference import load_source_subtitle, references_by_asr_segment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "auto"
DEFAULT_PREFERRED_HELPER_MODEL = "qwen-mt-plus"
QWEN_MT_PREFIX = "qwen-mt-"
QWEN_MT_TARGET_LANG = "Chinese"
ALIYUN_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$", re.IGNORECASE)
ALLOWED_ALIYUN_REGIONS = {"cn-beijing"}
QWEN_MT_SOURCE_LANG_MAP = {
    "english": "English",
    "en": "English",
    "french": "French",
    "fr": "French",
    "spanish": "Spanish",
    "es": "Spanish",
    "italian": "Italian",
    "it": "Italian",
}


@dataclass
class DisplayToken:
    text: str
    norm: str
    start: int
    end: int
    raw_start: int | None = None
    raw_end: int | None = None


@dataclass
class SegmentChunk:
    key: str
    source_raw: str
    source_display: str
    reference_used: bool = False


CJK_RANGES = (
    "\u3400-\u4dbf"  # CJK Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\u3040-\u30ff"  # Hiragana / Katakana
    "\uac00-\ud7af"  # Hangul syllables
)
# CJK runs are tokenized per character, which keeps subtitle wrapping and
# max-display-token splitting meaningful for Chinese/Japanese/Korean source text.
TOKEN_RE = re.compile(
    r"[0-9]+(?:[:.,][0-9]+)*%?"
    r"|[A-Za-zÀ-ÖØ-öø-ÿ]+(?:[’'][A-Za-zÀ-ÖØ-öø-ÿ]+)?"
    rf"|[{CJK_RANGES}]"
)
SENTENCE_BREAK_RE = re.compile(r"[.!?;:。！？；：]")
COMMA_MARKS = (",", "，", "、")
DISPLAY_TRIM_CHARS = " ,;:，。！？；：、"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate segments.txt from Fun-ASR transcript using DashScope translation.")
    parser.add_argument("transcript_words", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument(
        "--env",
        type=Path,
        default=None,
        help=(
            "Explicit local .env path. Omit to discover one: $ALIYUN_ENV_FILE, then the nearest "
            ".env at or above the Skill directory and the current directory that declares a "
            "provider credential key."
        ),
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        choices=[DEFAULT_MODEL, DEFAULT_PREFERRED_HELPER_MODEL],
        help=(
            "Fixed translation model. Both 'auto' and 'qwen-mt-plus' resolve to qwen-mt-plus."
        ),
    )
    parser.add_argument("--source-language-name", default="source-language")
    parser.add_argument("--domain-name", default="finance/trading training videos")
    parser.add_argument(
        "--translation-context",
        type=Path,
        required=True,
        help="Validated whole-source context containing Qwen-MT domains, terms, and translation memory.",
    )
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel DashScope translation batches. Production default is 1 because qwen-mt-plus is more stable in serial mode.")
    parser.add_argument("--heartbeat-seconds", type=int, default=60, help="Print progress at least this often while translating batches.")
    parser.add_argument("--screen-context", type=Path, default=None, help="Optional screen context file.")
    parser.add_argument("--source-subtitle", type=Path, default=None, help="Optional original-language SRT/VTT used only to correct ASR display text.")
    parser.add_argument("--max-display-tokens", type=int, default=18)
    parser.add_argument("--max-retries", type=int, default=8, help="Retries per translation request, including rate-limit backoff.")
    parser.add_argument(
        "--qwen-mt-min-interval-seconds",
        type=float,
        default=1.0,
        help="Minimum interval between qwen-mt requests. Keep production runs at 1 second or higher.",
    )
    parser.add_argument(
        "--confirm-external-processing",
        action="store_true",
        help="Required acknowledgement before transcript text is sent to Alibaba qwen-mt-plus.",
    )
    return parser.parse_args()


def resolve_env(explicit: Path | None) -> dict[str, str]:
    """Effective provider environment: the local .env merged with the process.

    The real environment wins, so an exported credential keeps working, and the
    file supplies whatever the process does not define. Discovery is anchored on
    the Skill directory, so it does not depend on the caller's working directory.
    """
    search = prepare_env_file(explicit, start=Path(__file__).resolve().parent)
    file_env = read_env_file(search.path) if search.path is not None else {}
    return {**file_env, **os.environ}


def resolve_helper_model(requested: str) -> tuple[str, str]:
    requested = (requested or "").strip()
    if requested not in {DEFAULT_MODEL, DEFAULT_PREFERRED_HELPER_MODEL}:
        raise RuntimeError("Only the fixed qwen-mt-plus translation model is allowed.")
    return DEFAULT_PREFERRED_HELPER_MODEL, "fixed-default"


def token_norm(text: str) -> str:
    """Normalize a display token or ASR word for alignment.

    Unicode word characters are kept, so CJK text survives normalization. A
    Latin-only whitelist erased every Chinese/Japanese/Korean word, which made
    the chunker drop them from the delivered subtitles.
    """
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE).lower()


def display_tokens(text: str) -> list[DisplayToken]:
    return [
        DisplayToken(match.group(0), token_norm(match.group(0)), match.start(), match.end())
        for match in TOKEN_RE.finditer(text)
        if token_norm(match.group(0))
    ]


def align_tokens_to_raw(tokens: list[DisplayToken], raw_words: list[str]) -> bool:
    """Map every display token onto the ASR words it consumes.

    Alignment runs on a flattened normalized word stream, so a token may begin
    or end inside a word while still owning that whole word. The previous
    exact-equality accumulation could not express a CJK ASR word spanning
    several single-character display tokens; it returned False, and the caller
    then discarded the segment's trailing ASR words instead of failing loudly.
    """
    flat_parts: list[str] = []
    owners: list[int] = []
    for index, word in enumerate(raw_words):
        norm = token_norm(word)
        flat_parts.append(norm)
        owners.extend([index] * len(norm))
    flat = "".join(flat_parts)
    if not flat:
        return False
    cursor = 0
    for token in tokens:
        if not token.norm:
            return False
        position = flat.find(token.norm, cursor)
        if position < 0:
            return False
        token.raw_start = owners[position]
        token.raw_end = owners[position + len(token.norm) - 1] + 1
        cursor = position + len(token.norm)
    return True


def token_boundaries(text: str, tokens: list[DisplayToken], max_tokens: int) -> list[tuple[int, int]]:
    if not tokens:
        return []
    boundaries = [0]
    for i in range(len(tokens) - 1):
        gap = text[tokens[i].end : tokens[i + 1].start]
        if SENTENCE_BREAK_RE.search(gap) or (
            any(mark in gap for mark in COMMA_MARKS) and i + 1 - boundaries[-1] >= 8
        ):
            boundaries.append(i + 1)
    boundaries.append(len(tokens))

    pieces: list[tuple[int, int]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        if end - start <= max_tokens:
            pieces.append((start, end))
            continue
        cursor = start
        while cursor < end:
            next_end = min(end, cursor + max_tokens)
            pieces.append((cursor, next_end))
            cursor = next_end

    merged: list[tuple[int, int]] = []
    for start, end in pieces:
        if merged and end - start <= 3:
            prev_start, _prev_end = merged.pop()
            merged.append((prev_start, end))
        else:
            merged.append((start, end))
    return merged


def whole_segment_chunk(asr_index: int, text: str, raw_words: list[str]) -> list[SegmentChunk]:
    """Return one chunk owning every ASR word of the segment.

    This is the safe terminal state: when the display tokens cannot be aligned
    to the word table, the workflow keeps all words and falls back to the raw
    segment text instead of emitting a silently truncated subtitle line.
    """
    raw = " ".join(raw_words).strip()
    if not raw:
        return []
    return [SegmentChunk(f"S{asr_index:04d}_P01", raw, " ".join(text.split()))]


def chunk_segment(asr_index: int, text: str, raw_words: list[str], max_display_tokens: int) -> list[SegmentChunk]:
    tokens = display_tokens(text)
    aligned = align_tokens_to_raw(tokens, raw_words)
    if not tokens or not aligned:
        return whole_segment_chunk(asr_index, text, raw_words)

    chunks: list[SegmentChunk] = []
    for part_index, (token_start, token_end) in enumerate(token_boundaries(text, tokens, max_display_tokens), start=1):
        first = tokens[token_start]
        last = tokens[token_end - 1]
        if first.raw_start is None or last.raw_end is None:
            continue
        raw = " ".join(raw_words[first.raw_start : last.raw_end]).strip()
        display = text[first.start : last.end].strip(DISPLAY_TRIM_CHARS)
        if raw and display:
            chunks.append(SegmentChunk(f"S{asr_index:04d}_P{part_index:02d}", raw, display))
    # Never deliver a partial word span: verify that the chunks own every ASR
    # word before returning them, and fall back to the whole segment if not.
    if not chunks or " ".join(chunk.source_raw for chunk in chunks).split() != raw_words:
        return whole_segment_chunk(asr_index, text, raw_words)
    return chunks


def boundary_map(reference_norms: list[str], raw_norms: list[str], boundary: int) -> int:
    matcher = SequenceMatcher(None, reference_norms, raw_norms, autojunk=False)
    anchors: list[tuple[int, int]] = [(0, 0), (len(reference_norms), len(raw_norms))]
    for block in matcher.get_matching_blocks():
        anchors.extend([(block.a, block.b), (block.a + block.size, block.b + block.size)])
    anchors = sorted(set(anchors))
    left = max((anchor for anchor in anchors if anchor[0] <= boundary), default=(0, 0))
    right = min((anchor for anchor in anchors if anchor[0] >= boundary), default=(len(reference_norms), len(raw_norms)))
    if right[0] == left[0]:
        return max(0, min(len(raw_norms), left[1]))
    ratio = (boundary - left[0]) / (right[0] - left[0])
    mapped = round(left[1] + ratio * (right[1] - left[1]))
    return max(0, min(len(raw_norms), mapped))


def reference_text_for_raw_span(reference_text: str, raw_words: list[str]) -> str | None:
    """Crop a reference cue to the lexical span owned by one Fun-ASR segment.

    Reference cues often straddle ASR boundaries. They may correct spelling, but
    they must never lend leading/trailing words to the current ASR segment.
    """
    tokens = display_tokens(reference_text)
    raw_norms = [token_norm(word) for word in raw_words if token_norm(word)]
    reference_norms = [token.norm for token in tokens]
    if not tokens or not raw_norms:
        return None
    matcher = SequenceMatcher(None, reference_norms, raw_norms, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    if not blocks:
        return None
    matched = sum(block.size for block in blocks)
    minimum_matches = 1 if len(raw_norms) <= 2 else 2
    if matched < minimum_matches or matched / len(raw_norms) < 0.5:
        return None

    first = blocks[0]
    last = blocks[-1]
    leading_raw = first.b
    trailing_raw = len(raw_norms) - (last.b + last.size)
    candidate_start = max(0, first.a - leading_raw)
    candidate_end = min(len(tokens), last.a + last.size + trailing_raw)
    candidate_norms = reference_norms[candidate_start:candidate_end]
    if not candidate_norms:
        return None
    max_extra = max(2, math.ceil(len(raw_norms) * 0.25))
    if len(candidate_norms) > len(raw_norms) + max_extra:
        return None
    if abs(len(candidate_norms) - len(raw_norms)) > max_extra:
        return None
    if SequenceMatcher(None, candidate_norms, raw_norms, autojunk=False).ratio() < 0.55:
        return None
    start_char = tokens[candidate_start].start
    end_char = tokens[candidate_end - 1].end
    return reference_text[start_char:end_char].strip(DISPLAY_TRIM_CHARS)


def chunk_segment_with_reference(
    asr_index: int,
    reference_text: str,
    raw_words: list[str],
    max_display_tokens: int,
) -> list[SegmentChunk] | None:
    fitted_reference = reference_text_for_raw_span(reference_text, raw_words)
    if not fitted_reference:
        return None
    tokens = display_tokens(fitted_reference)
    raw_norms = [token_norm(word) for word in raw_words if token_norm(word)]
    reference_norms = [token.norm for token in tokens]
    if not tokens or not raw_norms:
        return None
    similarity = SequenceMatcher(None, reference_norms, raw_norms, autojunk=False).ratio()
    if similarity < 0.45:
        return None

    pieces = token_boundaries(fitted_reference, tokens, max_display_tokens)
    while len(pieces) > len(raw_words) and len(pieces) > 1:
        start, _end = pieces[-2]
        _next_start, end = pieces[-1]
        pieces[-2:] = [(start, end)]

    reference_boundaries = [pieces[0][0], *[end for _start, end in pieces]]
    raw_boundaries = [boundary_map(reference_norms, raw_norms, boundary) for boundary in reference_boundaries]
    raw_boundaries[0] = 0
    raw_boundaries[-1] = len(raw_words)
    for index in range(1, len(raw_boundaries)):
        raw_boundaries[index] = max(raw_boundaries[index], raw_boundaries[index - 1])

    chunks: list[SegmentChunk] = []
    for part_index, ((token_start, token_end), raw_start, raw_end) in enumerate(
        zip(pieces, raw_boundaries, raw_boundaries[1:]),
        start=1,
    ):
        if raw_end <= raw_start:
            continue
        display = fitted_reference[tokens[token_start].start : tokens[token_end - 1].end].strip(DISPLAY_TRIM_CHARS)
        raw = " ".join(raw_words[raw_start:raw_end]).strip()
        if not display or not raw:
            continue
        digest = hashlib.sha1(display.encode("utf-8")).hexdigest()[:8]
        chunks.append(SegmentChunk(f"S{asr_index:04d}_P{part_index:02d}_R{digest}", raw, display, True))
    if not chunks or " ".join(chunk.source_raw for chunk in chunks).split() != raw_words:
        return None
    return chunks


def build_chunks(
    transcript: dict,
    max_display_tokens: int,
    source_references: dict[int, str] | None = None,
    reference_stats: dict[str, object] | None = None,
) -> list[SegmentChunk]:
    chunks: list[SegmentChunk] = []
    fallback_chunks: list[SegmentChunk] = []
    reference_candidates = 0
    reference_used_segments = 0
    for asr_index, segment in enumerate(transcript.get("segments", []), start=1):
        text = " ".join(str(segment.get("text") or "").split())
        raw_words = [
            normalize_word(str(word.get("word") or word.get("text") or ""))
            for word in segment.get("words", [])
        ]
        raw_words = [word for word in raw_words if word]
        reference_text = (source_references or {}).get(asr_index - 1, "")
        fallback = chunk_segment(asr_index, text, raw_words, max_display_tokens)
        fallback_chunks.extend(fallback)
        referenced = (
            chunk_segment_with_reference(asr_index, reference_text, raw_words, max_display_tokens)
            if reference_text
            else None
        )
        if reference_text:
            reference_candidates += 1
        if referenced:
            reference_used_segments += 1
        chunks.extend(referenced or fallback)
    reference_disabled = reference_candidates >= 4 and reference_used_segments * 2 < reference_candidates
    if reference_disabled:
        chunks = fallback_chunks
        reference_used_segments = 0
    if reference_stats is not None:
        reference_stats.update(
            {
                "candidate_segments": reference_candidates,
                "corrected_segments": reference_used_segments,
                "reference_disabled": reference_disabled,
                "status": "disabled_as_anomalous" if reference_disabled else (
                    "applied_with_asr_boundaries" if reference_used_segments else "not_applied"
                ),
            }
        )
    return chunks


def extract_json_array(text: str) -> list[dict]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON array found in model response.")
    return json.loads(stripped[start : end + 1])


def is_qwen_mt_model(model: str) -> bool:
    return (model or "").strip().lower().startswith(QWEN_MT_PREFIX)


def qwen_mt_chat_url(env: dict[str, str]) -> str:
    workspace_id = (env.get("ALIYUN_WORKSPACE_ID") or "").strip()
    region = (env.get("ALIYUN_REGION") or "cn-beijing").strip() or "cn-beijing"
    if not workspace_id:
        raise RuntimeError("ALIYUN_WORKSPACE_ID is required when using qwen-mt-plus helper.")
    if not ALIYUN_IDENTIFIER_RE.fullmatch(workspace_id):
        raise RuntimeError("ALIYUN_WORKSPACE_ID must contain only letters, digits, and hyphens.")
    if region not in ALLOWED_ALIYUN_REGIONS:
        raise RuntimeError("ALIYUN_REGION must be cn-beijing for this fixed production workflow.")
    return f"https://{workspace_id}.{region}.maas.aliyuncs.com/compatible-mode/v1/chat/completions"


def untrusted_source_text(value: str) -> str:
    cleaned = "".join(character for character in value if character in "\n\t" or ord(character) >= 32).strip()
    if not cleaned:
        raise RuntimeError("Translation source text is empty after control-character filtering.")
    if len(cleaned) > 4_000:
        raise RuntimeError("Translation source text exceeds the per-segment safety limit.")
    return cleaned


def qwen_mt_source_lang(source_language_name: str) -> str:
    key = (source_language_name or "").strip().lower()
    return QWEN_MT_SOURCE_LANG_MAP.get(key, "auto")


def qwen_translation_options(source_language_name: str, translation_context: dict) -> dict:
    options = {
        "source_lang": qwen_mt_source_lang(source_language_name),
        "target_lang": QWEN_MT_TARGET_LANG,
        "domains": translation_context["domains"],
    }
    if translation_context.get("terms"):
        options["terms"] = translation_context["terms"]
    if translation_context.get("tm_list"):
        options["tm_list"] = translation_context["tm_list"]
    return options


def retry_delay_seconds(exc: Exception, attempt: int) -> float:
    """Respect a gateway-provided retry window and otherwise back off conservatively."""
    if isinstance(exc, urllib.error.HTTPError):
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass
    return min(120.0, 10.0 * (2 ** (attempt - 1)))


def dashscope_translate_qwen_mt(
    api_key: str,
    env: dict[str, str],
    model: str,
    item: SegmentChunk,
    max_retries: int,
    source_language_name: str,
    translation_context: dict,
) -> dict[str, str]:
    # Qwen-MT rejects system messages. The source is untrusted media data and is
    # accepted only as the translation model's source text, never as tool input.
    translation_options = qwen_translation_options(source_language_name, translation_context)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": untrusted_source_text(item.source_display)}],
        "translation_options": translation_options,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        qwen_mt_chat_url(env),
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return {item.key: str(content).strip()}
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < max_retries:
                wait = retry_delay_seconds(exc, attempt)
                print(f"Qwen-MT request failed ({exc}); retrying in {wait:.0f}s ({attempt}/{max_retries})...", flush=True)
                time.sleep(wait)
    raise RuntimeError(f"Qwen-MT translation failed after {max_retries} attempts: {last_error}")


def load_translation_context(path: Path) -> tuple[dict, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("stage") != "translation-context" or not str(payload.get("domains") or "").strip():
        raise RuntimeError("Translation context is missing validated source-analysis domains.")
    for name in ("terms", "tm_list"):
        value = payload.get(name)
        if not isinstance(value, list):
            raise RuntimeError(f"Translation context {name} must be a list.")
    return payload, hashlib.sha256(path.read_bytes()).hexdigest()


def load_cache(path: Path | None, context_sha256: str) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 2:
        return {}
    if payload.get("translation_context_sha256") != context_sha256:
        print("Ignoring translation cache created with a different whole-source context.", flush=True)
        return {}
    translations = payload.get("translations")
    return translations if isinstance(translations, dict) else {}


def optional_text(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_cache(path: Path | None, translations: dict[str, str], context_sha256: str) -> None:
    if not path:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = {
        "schema_version": 2,
        "translation_context_sha256": context_sha256,
        "translations": translations,
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def chunk_batches(items: list[SegmentChunk], batch_size: int) -> list[list[SegmentChunk]]:
    return [items[start : start + batch_size] for start in range(0, len(items), batch_size)]


def translate_pending(
    *,
    api_key: str,
    env: dict[str, str],
    model: str,
    pending: list[SegmentChunk],
    translations: dict[str, str],
    cache: Path | None,
    batch_size: int,
    concurrency: int,
    max_retries: int,
    source_language_name: str,
    domain_name: str,
    screen_context: str,
    heartbeat_seconds: int,
    qwen_mt_min_interval_seconds: float,
    translation_context: dict,
    translation_context_sha256: str,
) -> dict[str, str]:
    batches = chunk_batches(pending, batch_size)
    total = len(batches)
    if not batches:
        return translations

    concurrency = max(1, min(concurrency, total))
    print(
        f"DashScope segment generation: {len(pending)} pending chunks, {total} batches, "
        f"concurrency={concurrency}, batch_size={batch_size}; qwen-mt-plus production default is serial for rate-limit stability",
        flush=True,
    )

    completed = 0
    started_at = time.monotonic()
    last_heartbeat = started_at
    last_qwen_mt_request_at: float | None = None

    def run_model(used_model: str, batch: list[SegmentChunk]) -> dict[str, str]:
        nonlocal last_qwen_mt_request_at
        if not is_qwen_mt_model(used_model):
            raise RuntimeError("Only qwen-mt-plus is allowed for subtitle translation.")
        result: dict[str, str] = {}
        for item in batch:
            if last_qwen_mt_request_at is not None:
                remaining = qwen_mt_min_interval_seconds - (time.monotonic() - last_qwen_mt_request_at)
                if remaining > 0:
                    time.sleep(remaining)
            last_qwen_mt_request_at = time.monotonic()
            result.update(
                dashscope_translate_qwen_mt(
                    api_key,
                    env,
                    used_model,
                    item,
                    max_retries,
                    source_language_name,
                    translation_context,
                )
            )
        return result

    def translate_one(batch_index: int, batch: list[SegmentChunk]) -> tuple[int, dict[str, str]]:
        used_model = model
        result = run_model(used_model, batch)
        missing = [item.key for item in batch if item.key not in result]
        if missing:
            print(
                f"Batch {batch_index} missed {len(missing)} id(s); retrying missing items individually: {missing[:5]}",
                flush=True,
            )
            by_key = {item.key: item for item in batch}
            for key in missing:
                retry_item = by_key[key]
                retry = run_model(used_model, [retry_item])
                if key in retry:
                    result[key] = retry[key]
            missing = [item.key for item in batch if item.key not in result]
        if missing:
            raise RuntimeError(f"Model response missed ids after individual retry: {missing[:5]}")
        return batch_index, result

    if concurrency == 1:
        for batch_index, batch in enumerate(batches, start=1):
            print(f"Translating batch {batch_index}/{total}: {len(batch)} items", flush=True)
            _idx, result = translate_one(batch_index, batch)
            translations.update(result)
            save_cache(cache, translations, translation_context_sha256)
            completed += 1
            now = time.monotonic()
            elapsed = now - started_at
            avg = elapsed / max(completed, 1)
            remaining = max(total - completed, 0) * avg
            print(
                f"Progress: {completed}/{total} batches; elapsed {elapsed / 60:.1f} min; "
                f"estimated remaining {remaining / 60:.1f} min",
                flush=True,
            )
        return translations

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {
            executor.submit(translate_one, batch_index, batch): batch_index
            for batch_index, batch in enumerate(batches, start=1)
        }
        for future in as_completed(futures):
            batch_index, result = future.result()
            translations.update(result)
            save_cache(cache, translations, translation_context_sha256)
            completed += 1
            now = time.monotonic()
            elapsed = now - started_at
            avg = elapsed / max(completed, 1)
            remaining = max(total - completed, 0) * avg
            if now - last_heartbeat >= heartbeat_seconds or completed == total:
                print(
                    f"Progress: {completed}/{total} batches; last completed batch {batch_index}; "
                    f"elapsed {elapsed / 60:.1f} min; estimated remaining {remaining / 60:.1f} min",
                    flush=True,
                )
                last_heartbeat = now
    return translations


def write_segments(path: Path, chunks: list[SegmentChunk], translations: dict[str, str]) -> None:
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        zh = translations.get(chunk.key, "").strip()
        if not zh:
            zh = "[待翻译]"
        lines.extend(
            [
                f"[SEG {index:04d}]",
                f"SRC_RAW: {chunk.source_raw}",
                f"SRC_DISPLAY: {chunk.source_display}",
                f"ZH: {zh}",
                "[/SEG]",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()
    if not args.confirm_external_processing:
        raise SystemExit(
            "Refusing external processing without --confirm-external-processing. "
            "This sends subtitle text to Alibaba qwen-mt-plus."
        )
    env = resolve_env(args.env)
    api_key = env.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise SystemExit("DASHSCOPE_API_KEY is missing from the local .env and the process environment.")
    model, model_source = resolve_helper_model(args.model)
    if model_source == "fixed-default":
        print(f"Helper model: {model} (fixed production helper).", flush=True)
    else:
        print(f"Helper model: {model} (from {model_source})", flush=True)

    transcript = json.loads(args.transcript_words.read_text(encoding="utf-8"))
    source_references: dict[int, str] = {}
    if args.source_subtitle:
        cues = load_source_subtitle(args.source_subtitle)
        source_references = references_by_asr_segment(transcript, cues)
        print(
            f"Source subtitle reference: {args.source_subtitle}; "
            f"{len(cues)} cues mapped to {len(source_references)} ASR segments",
            flush=True,
        )
    reference_stats: dict[str, object] = {}
    chunks = build_chunks(transcript, args.max_display_tokens, source_references, reference_stats)
    if reference_stats.get("reference_disabled"):
        print(
            "Source subtitle reference was disabled because most mapped text failed lexical "
            "scope checks; using Fun-ASR text and boundaries only.",
            flush=True,
        )
    translation_context, translation_context_sha256 = load_translation_context(args.translation_context)
    translations = load_cache(args.cache, translation_context_sha256)
    print(f"Chunks: {len(chunks)}; cached translations: {len(translations)}", flush=True)

    screen_context = optional_text(args.screen_context)
    if screen_context:
        print(f"Screen context: {args.screen_context}", flush=True)

    pending = [chunk for chunk in chunks if chunk.key not in translations]
    translations = translate_pending(
        api_key=api_key,
        env=env,
        model=model,
        pending=pending,
        translations=translations,
        cache=args.cache,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        max_retries=args.max_retries,
        source_language_name=args.source_language_name,
        domain_name=args.domain_name,
        screen_context=screen_context,
        heartbeat_seconds=args.heartbeat_seconds,
        qwen_mt_min_interval_seconds=max(0.0, args.qwen_mt_min_interval_seconds),
        translation_context=translation_context,
        translation_context_sha256=translation_context_sha256,
    )

    write_segments(args.out, chunks, translations)
    meta = {
        "path": "helper_dashscope",
        "translation_provider": "qwen-mt-plus",
        "translation_path": "helper-dashscope",
        "model": model,
        "model_source": model_source,
        "fallback_model": "",
        "chunks": len(chunks),
        "cache": str(args.cache) if args.cache else "",
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "source_subtitle": str(args.source_subtitle) if args.source_subtitle else "",
        "source_subtitle_sha256": (
            hashlib.sha256(args.source_subtitle.read_bytes()).hexdigest()
            if args.source_subtitle
            else ""
        ),
        "reference_corrected_chunks": sum(1 for chunk in chunks if chunk.reference_used),
        "source_reference_status": reference_stats.get("status", "not_applied"),
        "source_reference_candidate_segments": reference_stats.get("candidate_segments", 0),
        "translation_context": str(args.translation_context.resolve()),
        "translation_context_sha256": translation_context_sha256,
        "domain_prompt_applied": True,
        "term_count": len(translation_context.get("terms") or []),
        "translation_memory_count": len(translation_context.get("tm_list") or []),
    }
    (args.out.parent / "segment_generation_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.out}", flush=True)
    print(f"Wrote {args.out.parent / 'segment_generation_meta.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
