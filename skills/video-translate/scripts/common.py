from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORD_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)

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
