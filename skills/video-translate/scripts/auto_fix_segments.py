#!/usr/bin/env python3
"""Deterministic subtitle segment auto-fixer.

Merges mechanically-fixable QA issues directly in segments.txt so that the AI
review step only has to handle real translation problems:

- isolated oral fillers (m/um/uh with filler-only Chinese) are absorbed silently
- dangling continuation fragments (of/into/to/... starts) merge into the previous cue
- flash subtitles (<0.8s, weak content) merge into a neighbor
- too-short cues (<0.5s) merge into a neighbor
- hanging connective boundaries (cue ends with and/to/of/...) merge with a short
  next cue when the merged cue still respects the viewing-rhythm guardrails

Every merge must keep the merged cue within the visual guardrails
(duration <= 8s, source words <= 24, wrapped Chinese lines <= 3), otherwise the
merge is skipped and the issue is left for AI review. SRC_RAW spans are only
concatenated, never rewritten, so the timestamp contract is preserved.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from common import (
    FILLER_RAW_TOKENS,
    FILLER_ZH_TEXT,
    HANGING_RAW_END_WORDS,
    HANGING_RAW_START_WORDS,
    align_segments,
    parse_segments,
    read_json,
    split_chinese_line,
    tokenize_raw,
)


MAX_MERGED_DURATION = 8.0
MAX_MERGED_SOURCE_WORDS = 24
MAX_MERGED_ZH_LINES = 3
ZH_CHARS_PER_LINE = 36


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministically merge mechanically-fixable subtitle segments.")
    parser.add_argument("word_table", type=Path)
    parser.add_argument("segments_txt", type=Path)
    parser.add_argument("--out", type=Path, default=None, help="Output path. Defaults to overwriting input (with .bak backup).")
    parser.add_argument("--dry-run", action="store_true", help="Only print planned merges.")
    parser.add_argument("--max-passes", type=int, default=5)
    return parser.parse_args()


def is_filler_zh(text: str) -> bool:
    return text.strip("。，,.！？!? ") in FILLER_ZH_TEXT


def join_zh(left: str, right: str, *, drop_right: bool = False, separator: str = "，") -> str:
    left = left.strip()
    right = "" if drop_right else right.strip()
    if not right:
        return left
    if not left:
        return right
    if left.endswith(("？", "！", "?", "!")):
        return f"{left}{right}"
    trimmed = left.rstrip("。，,. ")
    return f"{trimmed}{separator}{right}" if separator else f"{trimmed}{right}"


def join_display(left: str, right: str) -> str:
    left, right = left.strip(), right.strip()
    if not left:
        return right
    if not right:
        return left
    return f"{left} {right}"


class Cue:
    __slots__ = ("source_raw", "source_display", "translation", "start", "end")

    def __init__(self, source_raw: str, source_display: str, translation: str, start: float, end: float):
        self.source_raw = source_raw
        self.source_display = source_display
        self.translation = translation
        self.start = start
        self.end = end

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def word_count(self) -> int:
        return len(tokenize_raw(self.source_raw))

    @property
    def zh_lines(self) -> int:
        return len(split_chinese_line(self.translation, ZH_CHARS_PER_LINE))


def merged_within_guardrails(a: Cue, b: Cue, zh: str) -> bool:
    duration = b.end - a.start
    words = a.word_count + b.word_count
    lines = len(split_chinese_line(zh, ZH_CHARS_PER_LINE))
    return duration <= MAX_MERGED_DURATION and words <= MAX_MERGED_SOURCE_WORDS and lines <= MAX_MERGED_ZH_LINES


def merge(cues: list[Cue], i: int, j: int, zh: str) -> None:
    """Merge cues[i] and cues[j] (j = i+1) into position i with the given Chinese."""
    a, b = cues[i], cues[j]
    a.source_raw = f"{a.source_raw} {b.source_raw}".strip()
    a.source_display = join_display(a.source_display, b.source_display)
    a.translation = zh
    a.end = b.end
    del cues[j]


def find_fix(cues: list[Cue]) -> tuple[int, int, str, str] | None:
    """Return (left_index, right_index, merged_zh, reason) for the first applicable merge."""
    for i, cue in enumerate(cues):
        tokens = tokenize_raw(cue.source_raw)
        if not tokens:
            continue
        prev_i = i - 1 if i > 0 else None
        next_i = i + 1 if i + 1 < len(cues) else None

        # 1. Isolated oral filler: absorb silently into a neighbor.
        if (
            cue.duration < 0.8
            and 1 <= len(tokens) <= 2
            and all(token in FILLER_RAW_TOKENS for token in tokens)
            and is_filler_zh(cue.translation)
        ):
            if prev_i is not None:
                zh = cues[prev_i].translation
                if merged_within_guardrails(cues[prev_i], cue, zh):
                    return prev_i, i, zh, "isolated_filler"
            if next_i is not None:
                zh = cues[next_i].translation
                if merged_within_guardrails(cue, cues[next_i], zh):
                    return i, next_i, zh, "isolated_filler"

        # 2. Continuation fragment: of/into/to/... start, short span -> merge into previous.
        if prev_i is not None and tokens[0] in HANGING_RAW_START_WORDS and len(tokens) <= 6:
            zh = join_zh(cues[prev_i].translation, cue.translation, separator="")
            if merged_within_guardrails(cues[prev_i], cue, zh):
                return prev_i, i, zh, "continuation_fragment"

        # 3. Flash subtitle: <0.8s and weak content -> merge into a neighbor.
        if cue.duration < 0.8 and (len(cue.translation) <= 8 or len(tokens) <= 4):
            if prev_i is not None:
                zh = join_zh(cues[prev_i].translation, cue.translation)
                if merged_within_guardrails(cues[prev_i], cue, zh):
                    return prev_i, i, zh, "flash_subtitle"
            if next_i is not None:
                zh = join_zh(cue.translation, cues[next_i].translation)
                if merged_within_guardrails(cue, cues[next_i], zh):
                    return i, next_i, zh, "flash_subtitle"

        # 4. Too-short cue: below minimum readable duration.
        if 0 < cue.duration < 0.5:
            if prev_i is not None:
                zh = join_zh(cues[prev_i].translation, cue.translation)
                if merged_within_guardrails(cues[prev_i], cue, zh):
                    return prev_i, i, zh, "short_duration"
            if next_i is not None:
                zh = join_zh(cue.translation, cues[next_i].translation)
                if merged_within_guardrails(cue, cues[next_i], zh):
                    return i, next_i, zh, "short_duration"

        # 5. Hanging connective boundary: ends with and/to/of/... and next cue is short.
        if (
            next_i is not None
            and tokens[-1] in HANGING_RAW_END_WORDS
            and cues[next_i].word_count <= 8
        ):
            zh = join_zh(cue.translation, cues[next_i].translation, separator="")
            if merged_within_guardrails(cue, cues[next_i], zh):
                return i, next_i, zh, "hanging_source_boundary"

    return None


def render_segments(cues: list[Cue]) -> str:
    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"[SEG {i:04d}]\n"
            f"SRC_RAW: {cue.source_raw}\n"
            f"SRC_DISPLAY: {cue.source_display}\n"
            f"ZH: {cue.translation}\n"
            f"[/SEG]\n"
        )
    return "\n".join(blocks)


def main() -> int:
    args = parse_args()
    table = read_json(args.word_table)
    text = args.segments_txt.read_text(encoding="utf-8")
    segments = parse_segments(text)
    aligned, failures = align_segments(table, segments)
    if failures:
        print(f"Cannot auto-fix: {len(failures)} segment(s) fail SRC_RAW alignment. Run validate_segments.py first.")
        return 2

    cues = [
        Cue(seg["source_raw"], seg["source_display"], seg["translation"], float(seg["start"]), float(seg["end"]))
        for seg in aligned
    ]

    actions: list[str] = []
    for _pass in range(args.max_passes):
        changed = False
        while True:
            fix = find_fix(cues)
            if fix is None:
                break
            left, right, zh, reason = fix
            actions.append(
                f"[{reason}] merge cue {left + 1} + {right + 1}: "
                f"`{cues[left].source_raw[-40:]}` + `{cues[right].source_raw[:40]}`"
            )
            merge(cues, left, right, zh)
            changed = True
        if not changed:
            break

    if not actions:
        print("Auto-fix: nothing to merge.")
        return 0

    print(f"Auto-fix: {len(actions)} merge(s) planned:")
    counts: dict[str, int] = {}
    for action in actions:
        kind = action.split("]")[0].strip("[")
        counts[kind] = counts.get(kind, 0) + 1
        print(f"- {action}")
    print("Summary: " + ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items())))

    if args.dry_run:
        print("Dry run only; segments.txt not modified.")
        return 0

    out = args.out or args.segments_txt
    if out == args.segments_txt:
        backup = args.segments_txt.with_suffix(args.segments_txt.suffix + ".bak")
        backup.write_text(text, encoding="utf-8")
        print(f"Backup: {backup}")
    out.write_text(render_segments(cues), encoding="utf-8")
    print(f"Wrote {out} ({len(cues)} segments after merge)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
