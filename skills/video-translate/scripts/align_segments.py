#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import align_segments, parse_segments, read_json, write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Align AI subtitle segments to word timestamps.")
    parser.add_argument("word_table", type=Path)
    parser.add_argument("segments_txt", type=Path)
    parser.add_argument("--out", type=Path, default=Path("runs/work/aligned_segments.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    table = read_json(args.word_table)
    segments = parse_segments(args.segments_txt.read_text(encoding="utf-8"))
    aligned, failures = align_segments(table, segments)
    write_json(args.out, {"segments": aligned, "failures": failures, "total_words": len(table)})
    print(f"Wrote {args.out}")
    print(f"Matched {len(aligned)} segments, failed {len(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
