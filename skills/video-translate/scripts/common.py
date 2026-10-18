from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)

# Provider credentials this Skill needs, and how the local env file is located.
# Characters that change the meaning of a URL (or are ambiguous in one). A filename
# containing them is fine on disk but must never become part of a URL.
URL_SIGNIFICANT_CHARACTERS = "#?%&+="


def url_significant_characters(text: str) -> list[str]:
    """Return the URL-significant characters present in ``text``, in a stable order."""
    return [character for character in URL_SIGNIFICANT_CHARACTERS if character in text]


CREDENTIAL_KEYS = ("DASHSCOPE_API_KEY", "ALIYUN_WORKSPACE_ID")
ENV_FILE_VARIABLE = "ALIYUN_ENV_FILE"
ENV_SEARCH_DEPTH = 4

# Shared token sets used by QA checks and the deterministic auto-fixer.
FILLER_RAW_TOKENS = {"m", "um", "uh", "er", "eh", "hmm"}
FILLER_ZH_TEXT = {"嗯", "呃", "啊", "唔"}
HANGING_RAW_START_WORDS = {"of", "into", "to", "with", "for", "from"}
HANGING_RAW_END_WORDS = {
    "and",
    "or",
    "but",
    "because",
    "so",
    "that",
    "which",
    "who",
    "when",
    "where",
    "while",
    "if",
    "then",
    "than",
    "to",
    "of",
    "for",
    "with",
    "are",
    "is",
    "was",
    "were",
}
SEGMENT_RE = re.compile(
    r"\[SEG\s+(\d+)\](.*?)(?:\[/SEG\]|(?=\n\[SEG\s+\d+\])|\Z)",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class Segment:
    index: int
    source_raw: str
    source_display: str
    translation: str


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_word(text: str) -> str:
    text = text.strip().lower().replace("’", "'")
    match = WORD_RE.search(text)
    return match.group(0) if match else ""


def tokenize_raw(text: str) -> list[str]:
    return [normalize_word(match.group(0)) for match in WORD_RE.finditer(text)]


def words_from_transcript(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "words" in data:
        return list(data["words"])

    words: list[dict[str, Any]] = []
    for segment in data.get("segments", []):
        for word in segment.get("words", []):
            words.append(word)
    return words


def build_word_table(words: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    table: list[dict[str, Any]] = []
    for i, word in enumerate(words):
        text = str(word.get("word") or word.get("text") or "").strip()
        if not text:
            continue
        norm = normalize_word(text)
        if not norm:
            continue
        table.append(
            {
                "id": len(table),
                "text": text,
                "norm": norm,
                "start": float(word["start"]),
                "end": float(word["end"]),
            }
        )
    return table


def word_stream(table: list[dict[str, Any]]) -> str:
    return " ".join(word["norm"] or normalize_word(word["text"]) for word in table).strip()


def parse_segments(text: str) -> list[Segment]:
    segments: list[Segment] = []
    for match in SEGMENT_RE.finditer(text):
        index = int(match.group(1))
        body = match.group(2)
        fields: dict[str, str] = {}
        for key in ("SRC_RAW", "SRC_DISPLAY", "ZH"):
            value_match = re.search(rf"^{key}:\s*(.*?)\s*$", body, re.MULTILINE)
            if value_match:
                fields[key] = value_match.group(1).strip()
        missing = [key for key in ("SRC_RAW", "SRC_DISPLAY", "ZH") if not fields.get(key)]
        if missing:
            raise ValueError(f"SEG {index:04d} is missing fields: {', '.join(missing)}")
        segments.append(
            Segment(
                index=index,
                source_raw=fields["SRC_RAW"],
                source_display=fields["SRC_DISPLAY"],
                translation=fields["ZH"],
            )
        )
    if not segments:
        raise ValueError("No [SEG 0001]...[/SEG] blocks found.")
    return segments


def find_contiguous_match(
    table: list[dict[str, Any]],
    source_raw: str,
    start_at: int = 0,
) -> tuple[int, int] | None:
    tokens = [token for token in tokenize_raw(source_raw) if token]
    if not tokens:
        return None

    norms = [word.get("norm") or normalize_word(word["text"]) for word in table]
    last_start = len(norms) - len(tokens)
    for i in range(start_at, last_start + 1):
        if norms[i : i + len(tokens)] == tokens:
            return i, i + len(tokens) - 1
    return None


def token_edit_distance(a: list[str], b: list[str], limit: int) -> int:
    """Levenshtein distance between token lists, early-exiting above `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, start=1):
        current = [i]
        row_min = i
        for j, token_b in enumerate(b, start=1):
            cost = 0 if token_a == token_b else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def find_fuzzy_match(
    table: list[dict[str, Any]],
    source_raw: str,
    start_at: int = 0,
    max_edits: int = 2,
    horizon: int = 800,
) -> tuple[int, int, int] | None:
    """Find the closest near-miss window for a SRC_RAW that failed exact matching.

    Returns (start_index, end_index, edit_distance) for the best window whose
    token edit distance to the SRC_RAW tokens is <= max_edits, searching from
    `start_at` up to `start_at + horizon` words. Returns None when nothing is
    close enough. Used to auto-repair SRC_RAW lines that a model lightly
    rewrote (typo, dropped/added word) instead of copying verbatim.
    """
    tokens = [token for token in tokenize_raw(source_raw) if token]
    if not tokens:
        return None

    norms = [word.get("norm") or normalize_word(word["text"]) for word in table]
    best: tuple[int, int, int] | None = None
    search_end = min(len(norms), start_at + horizon)
    for length in range(max(1, len(tokens) - max_edits), len(tokens) + max_edits + 1):
        for i in range(start_at, search_end - length + 1):
            window = norms[i : i + length]
            # Cheap prefilter: require some anchor overlap before running the DP.
            if window[0] != tokens[0] and window[-1] != tokens[-1]:
                continue
            edits = token_edit_distance(window, tokens, max_edits)
            if edits <= max_edits and (best is None or edits < best[2]):
                best = (i, i + length - 1, edits)
                if edits == 0:
                    return best
    return best


def align_segments(
    table: list[dict[str, Any]],
    segments: list[Segment],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aligned: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    cursor = 0

    for segment in segments:
        match = find_contiguous_match(table, segment.source_raw, cursor)
        if not match:
            failures.append(
                {
                    "index": segment.index,
                    "source_raw": segment.source_raw,
                    "reason": "No contiguous normalized word match found after current cursor.",
                }
            )
            continue

        start_i, end_i = match
        first_word = table[start_i]
        last_word = table[end_i]
        aligned.append(
            {
                "index": segment.index,
                "start": first_word["start"],
                "end": last_word["end"],
                "source_raw": segment.source_raw,
                "source_display": segment.source_display,
                "translation": segment.translation,
                "word_start_id": first_word["id"],
                "word_end_id": last_word["id"],
            }
        )
        cursor = end_i + 1

    return aligned, failures


def srt_time(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, rem = divmod(millis, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def ass_time(seconds: float) -> str:
    centis = round(seconds * 100)
    hours, rem = divmod(centis, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def char_width(char: str) -> float:
    if unicodedata.east_asian_width(char) in {"F", "W"}:
        return 1.0
    if char.isspace():
        return 0.35
    return 0.55


def text_width(text: str) -> float:
    return sum(char_width(char) for char in text)


def visible_len(text: str) -> int:
    return len("".join(char for char in text if not char.isspace()))



def display_chinese_subtitle_text(text: str) -> str:
    text = " ".join(text.split()).strip()
    while text.endswith(("，", "。", ",", ".")):
        text = text[:-1].rstrip()
    return text

def split_chinese_line(
    text: str,
    max_width: float = 36.0,
    adjustment: float = 3.0,
    prefer_semantic_breaks: bool = False,
) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return [""]
    if text_width(text) <= max_width:
        return [text]

    break_chars = "，。；、：！？,. ;:!?"
    min_tail_width = 8.0
    lines: list[str] = []
    remaining = text
    while text_width(remaining) > max_width + adjustment:
        width = 0.0
        hard_index = 0
        candidates: list[tuple[float, int]] = []
        earlier_punctuation: list[tuple[float, int]] = []
        for index, char in enumerate(remaining):
            width += char_width(char)
            if width <= max_width:
                hard_index = index + 1
            if char in break_chars:
                cut = index + 1
                if max_width - adjustment <= width <= max_width + adjustment:
                    candidates.append((abs(width - max_width), cut))
                elif 8.0 <= width < max_width - adjustment:
                    earlier_punctuation.append((width, cut))
            if width > max_width + adjustment:
                break

        if candidates:
            _distance, cut = min(candidates, key=lambda item: item[0])
        elif prefer_semantic_breaks and earlier_punctuation:
            _width, cut = max(earlier_punctuation, key=lambda item: item[0])
        else:
            cut = max(1, hard_index)
            if cut < len(remaining) and remaining[cut] in break_chars:
                cut += 1

        tail = remaining[cut:].strip()
        if text_width(tail) < min_tail_width:
            earlier = [item for item in earlier_punctuation if item[1] < cut]
            if earlier:
                _width, cut = max(earlier, key=lambda item: item[0])
            elif prefer_semantic_breaks:
                # Portrait subtitles favor a balanced extra line over a 1-3
                # character tail that would otherwise be merged back.
                max_balanced_cut = max(1, len(remaining) - int(min_tail_width))
                cut = min(cut, max_balanced_cut)

        line = remaining[:cut].strip()
        if line:
            lines.append(line)
        remaining = remaining[cut:].strip()

    if remaining:
        lines.append(remaining)

    if not prefer_semantic_breaks and len(lines) > 1 and visible_len(lines[-1]) < 4:
        tail = lines.pop()
        lines[-1] = f"{lines[-1]}{tail}"

    return lines or [text]


def split_source_line(text: str, max_width: float | None) -> list[str]:
    text = " ".join(text.split()).strip()
    if not text or max_width is None or text_width(text) <= max_width:
        return [text]

    words = text.split()
    lines: list[str] = []
    remaining = words
    while remaining and text_width(" ".join(remaining)) > max_width:
        fitting = 1
        punctuation_cut = 0
        for index in range(1, len(remaining) + 1):
            candidate = " ".join(remaining[:index])
            if text_width(candidate) > max_width:
                break
            fitting = index
            if candidate.rstrip().endswith((",", ";", ":", ".", "!", "?")):
                punctuation_cut = index
        cut = punctuation_cut if punctuation_cut and punctuation_cut >= max(1, fitting // 2) else fitting
        tail = " ".join(remaining[cut:])
        if tail and text_width(tail) < max_width * 0.25 and cut > 1:
            cut -= 1
        lines.append(" ".join(remaining[:cut]))
        remaining = remaining[cut:]
    if remaining:
        lines.append(" ".join(remaining))
    return lines or [text]


@dataclass(frozen=True)
class EnvFileSearch:
    """Outcome of locating the local env file that holds provider credentials.

    ``path`` is the file to use, or None when nothing usable was found.
    ``candidates`` lists every path that was considered, in order, so a caller
    can report exactly where it looked instead of failing silently.
    ``explicit`` distinguishes a caller-named path from a discovered one.
    """

    path: Path | None
    candidates: tuple[Path, ...]
    explicit: bool


def parse_env_text(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def read_env_file(path: Path) -> dict[str, str]:
    try:
        return parse_env_text(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def env_file_declares_credentials(path: Path) -> bool:
    """True when the file names at least one credential this Skill needs.

    This guards the upward search: an unrelated project's `.env` must never be
    adopted silently just because it happens to sit in a parent directory.
    """
    return any(key in read_env_file(path) for key in CREDENTIAL_KEYS)


def env_search_candidates(*, start: Path | None = None, depth: int = ENV_SEARCH_DEPTH) -> list[Path]:
    """`.env` paths to consider, nearest first, anchored on `start` then the cwd."""
    anchors = [Path(start)] if start is not None else []
    anchors.append(Path.cwd())
    candidates: list[Path] = []
    for anchor in anchors:
        try:
            resolved = anchor.resolve()
        except OSError:
            continue
        for base in [resolved, *list(resolved.parents)[:depth]]:
            candidate = base / ".env"
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def search_env_file(explicit: Path | None = None, *, start: Path | None = None) -> EnvFileSearch:
    """Locate the local `.env` used for provider credentials.

    Order: an explicit path, then ``$ALIYUN_ENV_FILE``, then the nearest `.env`
    at or above `start` (normally the script directory, which keeps discovery
    independent of the caller's working directory) followed by the current
    directory. A discovered file is used only when it declares a credential key.

    A caller-named file is reported as missing rather than ignored, so the
    caller can fail loudly instead of continuing without credentials.
    """
    named = explicit
    if named is None:
        from_environment = (os.environ.get(ENV_FILE_VARIABLE) or "").strip()
        if from_environment:
            named = Path(from_environment)
    if named is not None:
        resolved_named = named.expanduser()
        usable = resolved_named if resolved_named.is_file() else None
        return EnvFileSearch(path=usable, candidates=(resolved_named,), explicit=True)

    candidates = env_search_candidates(start=start)
    for candidate in candidates:
        if candidate.is_file() and env_file_declares_credentials(candidate):
            return EnvFileSearch(path=candidate, candidates=tuple(candidates), explicit=False)
    return EnvFileSearch(path=None, candidates=tuple(candidates), explicit=False)


def apply_env_file(path: Path) -> None:
    """Export an env file into the process; real environment values win."""
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


def describe_env_search(search: EnvFileSearch) -> str:
    if search.path is not None:
        return f"Using env file: {search.path}"
    if search.explicit:
        return (
            f"Env file not found: {search.candidates[0]}. "
            "Create it, or omit the explicit path so the local .env is discovered automatically."
        )
    listed = "\n".join(f"  - {candidate}" for candidate in search.candidates)
    return f"No .env with provider credentials was found. Searched:\n{listed}"


def prepare_env_file(
    explicit: Path | None = None,
    *,
    start: Path | None = None,
    report: bool = True,
) -> EnvFileSearch:
    """Locate the local env file, export it, and report which file was used.

    The single entry point for every script that needs provider credentials, so
    they all resolve the same file in the same order and none of them fails
    silently when it is absent.
    """
    search = search_env_file(explicit, start=start)
    if search.path is not None:
        apply_env_file(search.path)
        if report:
            print(f"Using env file: {search.path}", flush=True)
    elif report:
        print(describe_env_search(search), file=sys.stderr)
    return search
