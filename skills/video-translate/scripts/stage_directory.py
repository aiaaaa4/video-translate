#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from pathlib import Path


MARKER_NAME = ".video-translate-stage.json"


def reset_stage_directory(path: Path, stage: str, *, preserve: set[str] | None = None) -> Path:
    """Reset only a directory created and marked for this workflow stage."""
    resolved = path.expanduser().resolve()
    marker = resolved / MARKER_NAME
    preserved = set(preserve or ()) | {MARKER_NAME}

    if resolved.exists():
        if not resolved.is_dir():
            raise RuntimeError(f"Stage output is not a directory: {resolved}")
        entries = list(resolved.iterdir())
        if entries and not marker.is_file():
            raise RuntimeError(
                f"Refusing to reset non-empty unmarked directory: {resolved}. "
                "Choose a new stage output directory."
            )
        if marker.is_file():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"Invalid workflow stage marker: {marker}") from exc
            if payload.get("stage") != stage:
                raise RuntimeError(
                    f"Refusing to reset stage directory marked for {payload.get('stage')!r}: {resolved}"
                )
        for child in entries:
            if child.name in preserved:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        resolved.mkdir(parents=True)

    marker.write_text(json.dumps({"stage": stage}, indent=2) + "\n", encoding="utf-8")
    return resolved
