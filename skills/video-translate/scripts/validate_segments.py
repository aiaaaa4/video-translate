#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

from common import align_segments, find_fuzzy_match, parse_segments, read_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate AI segment output against word_table.json.")
    parser.add_argument("word_table", type=Path)
    parser.add_argument("segments_txt", type=Path)
    parser.add_argument(
        "--auto-repair",
        action="store_true",
        help=(
            "When a SRC_RAW fails exact matching but a near-identical word window exists "
            "(<=2 token edits), rewrite SRC_RAW from the original word stream and save the "
            "repaired segments.txt (a .bak backup is kept)."
        ),
    )
    parser.add_argument(
        "--max-repair-edits",
        type=int,
        default=2,
        help="Maximum token edit distance allowed for --auto-repair (default 2).",
    )
    return parser.parse_args()


def replace_src_raw(text: str, seg_index: int, new_raw: str) -> str:
    pattern = re.compile(
        rf"(\[SEG\s+{seg_index:04d}\].*?^SRC_RAW:\s*)(.*?)\s*$",
        re.DOTALL | re.MULTILINE,
    )

    def _sub(match: re.Match) -> str:
        return f"{match.group(1)}{new_raw}"

    return pattern.sub(_sub, text, count=1)


def try_auto_repair(
    table: list[dict],
    segments_txt: Path,
    failures: list[dict],
    aligned: list[dict],
    max_edits: int,
) -> list[str]:
    """Rewrite lightly-mangled SRC_RAW lines from the original word stream."""
    aligned_by_index = {int(seg["index"]): seg for seg in aligned}
    text = segments_txt.read_text(encoding="utf-8")
    parsed = {seg.index: seg for seg in parse_segments(text)}
    repairs: list[str] = []

    for failure in failures:
        seg_index = int(failure["index"])
        # Search after the previous successfully aligned segment.
        cursor = 0
        for i in range(seg_index - 1, 0, -1):
            if i in aligned_by_index:
                cursor = int(aligned_by_index[i]["word_end_id"]) + 1
                break
        match = find_fuzzy_match(table, failure["source_raw"], cursor, max_edits=max_edits)
        if not match:
            continue
        start_i, end_i, edits = match
        new_raw = " ".join(
            (table[i].get("norm") or "") for i in range(start_i, end_i + 1)
        ).strip()
        if not new_raw or seg_index not in parsed:
            continue
        text = replace_src_raw(text, seg_index, new_raw)
        repairs.append(
            f"SEG {seg_index:04d}: repaired SRC_RAW ({edits} edit(s))\n"
            f"  before: {failure['source_raw']}\n"
            f"  after:  {new_raw}"
        )

    if repairs:
        backup = segments_txt.with_suffix(segments_txt.suffix + ".bak")
        backup.write_text(segments_txt.read_text(encoding="utf-8"), encoding="utf-8")
        segments_txt.write_text(text, encoding="utf-8")
    return repairs


def report(table: list[dict], segments_txt: Path) -> tuple[list[dict], list[dict], list[str], bool]:
    segments = parse_segments(segments_txt.read_text(encoding="utf-8"))
    aligned, failures = align_segments(table, segments)

    expected = list(range(1, len(segments) + 1))
    actual = [segment.index for segment in segments]
    numbering_ok = actual == expected

    coverage_failures: list[str] = []
    if aligned:
        if aligned[0]["word_start_id"] != 0:
            coverage_failures.append(f"Missing words before SEG {aligned[0]['index']:04d}.")
        for previous, current in zip(aligned, aligned[1:]):
            expected_start = previous["word_end_id"] + 1
            actual_start = current["word_start_id"]
            if actual_start != expected_start:
                skipped = table[expected_start:actual_start]
                skipped_text = " ".join(word["norm"] for word in skipped[:20])
                suffix = " ..." if len(skipped) > 20 else ""
                coverage_failures.append(
                    f"Missing words between SEG {previous['index']:04d} and SEG {current['index']:04d}: "
                    f"{skipped_text}{suffix}"
                )
        if aligned[-1]["word_end_id"] != len(table) - 1:
            skipped = table[aligned[-1]["word_end_id"] + 1 :]
            skipped_text = " ".join(word["norm"] for word in skipped[:20])
            suffix = " ..." if len(skipped) > 20 else ""
            coverage_failures.append(f"Missing words after SEG {aligned[-1]['index']:04d}: {skipped_text}{suffix}")

    return aligned, failures, coverage_failures, numbering_ok


def main() -> int:
    args = parse_args()
    table = read_json(args.word_table)

    aligned, failures, coverage_failures, numbering_ok = report(table, args.segments_txt)

    if failures and args.auto_repair:
        repairs = try_auto_repair(table, args.segments_txt, failures, aligned, args.max_repair_edits)
        if repairs:
            print(f"Auto-repaired {len(repairs)} SRC_RAW line(s) from the original word stream:")
            for entry in repairs:
                print(entry)
            print()
            aligned, failures, coverage_failures, numbering_ok = report(table, args.segments_txt)

    segments_count = len(aligned) + len(failures)
    print(f"Segments: {segments_count}")
    print(f"Matched:  {len(aligned)}")
    print(f"Failed:   {len(failures)}")
    print(f"Numbering consecutive from 0001: {'yes' if numbering_ok else 'no'}")
    print(f"Full word coverage: {'yes' if not coverage_failures and aligned else 'no'}")

    if failures:
        print("\nFailures:")
        for failure in failures[:20]:
            print(f"- SEG {failure['index']:04d}: {failure['reason']}")
            print(f"  {failure['source_raw']}")
            hint = find_fuzzy_match(table, failure["source_raw"], 0, max_edits=3)
            if hint:
                start_i, end_i, edits = hint
                closest = " ".join((table[i].get("norm") or "") for i in range(start_i, end_i + 1))
                print(f"  closest word-stream window ({edits} edit(s)): {closest}")

    if coverage_failures:
        print("\nCoverage failures:")
        for failure in coverage_failures[:20]:
            print(f"- {failure}")

    if not numbering_ok or failures or coverage_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
