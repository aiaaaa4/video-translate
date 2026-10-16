#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


REMOVE_FILE_GLOBS = [
    "transcript/api_upload_audio.*",
    "work/dashscope_translation_cache*.json",
    "work/*.before_*.txt",
    "work/*.bak",
    "work/*_raw_ocr*.txt",
    "work/*ocr_raw*.txt",
    "work/contact_sheet.*",
    "work/frame_*.jpg",
    "work/frame_*.png",
    "work/final_qa_prompt.txt",
    "**/.DS_Store",
]

REMOVE_DIR_GLOBS = [
    "work/frames",
    "work/screen_context_frames",
    "work/ocr_frames",
    "work/__pycache__",
    "transcript/__pycache__",
    "subtitles/__pycache__",
    "**/__pycache__",
]

KEEP_NOTES = [
    "transcript/transcript_words.json",
    "work/word_table.json",
    "work/word_stream.txt",
    "work/asr_segments_reference.txt",
    "work/screen_context.txt",
    "work/prompt.txt",
    "work/segments.txt",
    "work/aligned_segments.json",
    "work/final_qa_report.md",
    "subtitles/*.ass",
    "subtitles/*.srt",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean disposable files from a completed video subtitle run.")
    parser.add_argument("run_dir", type=Path, help="Run directory in an output project's .work/<run-id> folder.")
    parser.add_argument("--confirm", action="store_true", help="Actually delete files. Without this, only print a dry run.")
    parser.add_argument(
        "--acknowledge-run-id",
        default="",
        help="Required with --confirm. Must exactly match the final run-directory name.",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Also remove raw ASR SRT and prompt/cache files that are useful for debugging but not final delivery.",
    )
    return parser.parse_args()


def resolve_run_dir(path: Path) -> Path:
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def validate_run_dir(run_dir: Path) -> None:
    if run_dir.parent.name != ".work":
        raise SystemExit("Refusing cleanup outside an output project's .work/<run-id> directory.")
    marker = run_dir / "work" / "workflow_status.json"
    if not marker.is_file():
        raise SystemExit(f"Refusing cleanup because the workflow marker is missing: {marker}")


def collect_paths(run_dir: Path, aggressive: bool) -> list[Path]:
    candidates: set[Path] = set()
    file_globs = list(REMOVE_FILE_GLOBS)
    dir_globs = list(REMOVE_DIR_GLOBS)
    if aggressive:
        file_globs.extend(
            [
                "transcript/transcript_raw.srt",
                "work/prompt.txt",
                "work/word_stream.txt",
                "work/asr_segments_reference.txt",
            ]
        )

    for pattern in file_globs:
        candidates.update(path for path in run_dir.glob(pattern) if path.is_file() or path.is_symlink())
    for pattern in dir_globs:
        candidates.update(path for path in run_dir.glob(pattern) if path.is_dir())

    return sorted(candidates, key=lambda p: (len(p.parts), str(p)))


def path_size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file() or child.is_symlink():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def human_size(value: int) -> str:
    size = float(value)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def main() -> int:
    args = parse_args()
    run_dir = resolve_run_dir(args.run_dir)
    if not run_dir.exists() or not run_dir.is_dir():
        raise SystemExit(f"Run directory not found: {run_dir}")
    validate_run_dir(run_dir)
    if args.confirm and args.acknowledge_run_id != run_dir.name:
        raise SystemExit("Refusing deletion: --acknowledge-run-id must exactly match the selected run directory.")

    targets = collect_paths(run_dir, args.aggressive)
    total = sum(path_size(path) for path in targets)

    mode = "DELETE" if args.confirm else "DRY RUN"
    print(f"{mode}: {run_dir}")
    print(f"Targets: {len(targets)}; reclaimable: {human_size(total)}")
    if not targets:
        print("Nothing to clean.")
        return 0

    print("\nWill remove:")
    for path in targets:
        print(f"- {path.relative_to(run_dir)} ({human_size(path_size(path))})")

    print("\nKept by design:")
    for note in KEEP_NOTES:
        print(f"- {note}")

    if not args.confirm:
        print("\nDry run only. Re-run with --confirm --acknowledge-run-id <run-id> after the final subtitles are accepted.")
        return 0

    for path in sorted(targets, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    print(f"\nCleaned {len(targets)} target(s), reclaimed about {human_size(total)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
