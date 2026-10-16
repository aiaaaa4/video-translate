#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import build_word_table, normalize_word, read_json, word_stream, words_from_transcript, write_json


def segment_raw_words(segment: dict) -> str:
    words: list[str] = []
    for word in segment.get("words", []):
        token = normalize_word(str(word.get("word") or word.get("text") or ""))
        if token:
            words.append(token)
    return " ".join(words)


def asr_segment_reference(transcript: dict) -> str:
    lines = [
        "# ASR Segment Reference",
        "",
        "Use this as a checklist when creating `segments.txt`.",
        "These are machine ASR boundaries, not mandatory subtitle boundaries.",
        "You may split long ASR segments or merge very short oral fragments, but every word must remain in order.",
        "",
    ]
    for index, segment in enumerate(transcript.get("segments", []), start=1):
        raw = segment_raw_words(segment)
        if not raw:
            continue
        text = " ".join(str(segment.get("text") or raw).split())
        word_count = len(raw.split())
        start = segment.get("start")
        end = segment.get("end")
        time_range = ""
        if start is not None and end is not None:
            time_range = f" [{float(start):.2f}s -> {float(end):.2f}s]"
        lines.extend(
            [
                f"[ASR {index:04d}]{time_range} words={word_count}",
                f"RAW: {raw}",
                f"DISPLAY_HINT: {text}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract normalized word stream and word table.")
    parser.add_argument("transcript_words", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("runs/work"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    transcript = read_json(args.transcript_words)
    table = build_word_table(words_from_transcript(transcript))
    stream = word_stream(table)
    write_json(args.out_dir / "word_table.json", table)
    (args.out_dir / "word_stream.txt").write_text(stream + "\n", encoding="utf-8")
    (args.out_dir / "asr_segments_reference.txt").write_text(asr_segment_reference(transcript), encoding="utf-8")
    print(f"Wrote {len(table)} words to {args.out_dir / 'word_table.json'}")
    print(f"Wrote {args.out_dir / 'word_stream.txt'}")
    print(f"Wrote {args.out_dir / 'asr_segments_reference.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
