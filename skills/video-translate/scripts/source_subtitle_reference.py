#!/usr/bin/env python3
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path


TIMESTAMP_RE = re.compile(
    r"(?P<start>(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{2,}:)?\d{2}:\d{2}[,.]\d{3})"
)
TAG_RE = re.compile(r"<[^>]+>")
MIN_TEMPORAL_OVERLAP_RATIO = 0.65


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


def timestamp_seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = "0"
        minutes, remainder = parts
    else:
        hours, minutes, remainder = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(remainder)


def clean_text(lines: list[str]) -> str:
    value = " ".join(line.strip() for line in lines if line.strip())
    value = value.replace(r"\N", " ").replace(r"\n", " ")
    return " ".join(html.unescape(TAG_RE.sub("", value)).split())


def load_source_subtitle(path: Path) -> list[SubtitleCue]:
    if path.suffix.lower() not in {".srt", ".vtt"}:
        raise RuntimeError("Source subtitle reference must be SRT or VTT.")
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[SubtitleCue] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        timestamp_index = next((index for index, line in enumerate(lines) if TIMESTAMP_RE.search(line)), None)
        if timestamp_index is None:
            continue
        match = TIMESTAMP_RE.search(lines[timestamp_index])
        if not match:
            continue
        cue_text = clean_text(lines[timestamp_index + 1 :])
        start = timestamp_seconds(match.group("start"))
        end = timestamp_seconds(match.group("end"))
        if cue_text and end > start:
            cues.append(SubtitleCue(start, end, cue_text))
    if not cues:
        raise RuntimeError(f"No valid subtitle cues were found: {path}")
    return cues


def references_by_asr_segment(transcript: dict, cues: list[SubtitleCue]) -> dict[int, str]:
    segments = transcript.get("segments", [])
    assigned: dict[int, list[str]] = {}
    for index, segment in enumerate(segments):
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        segment_duration = end - start
        if segment_duration <= 0:
            continue
        for cue in cues:
            overlap = max(0.0, min(cue.end, end) - max(cue.start, start))
            cue_duration = cue.end - cue.start
            overlap_base = min(segment_duration, cue_duration)
            if overlap_base <= 0 or overlap / overlap_base < MIN_TEMPORAL_OVERLAP_RATIO:
                continue
            assigned.setdefault(index, []).append(cue.text)
    return {index: " ".join(texts) for index, texts in assigned.items() if texts}
