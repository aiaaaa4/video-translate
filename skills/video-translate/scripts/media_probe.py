#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


AUDIO_SUFFIXES = (".m4a", ".mp3", ".aac", ".wav", ".flac", ".ogg", ".opus")
VIDEO_SUFFIXES = (
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpeg",
    ".mpg", ".ts", ".mts", ".m2ts", ".flv", ".wmv", ".3gp",
)
AMBIGUOUS_SUFFIXES = frozenset({".webm"})


@dataclass(frozen=True)
class VideoGeometry:
    width: int
    height: int
    rotation: int
    display_width: int
    display_height: int


def _normalized_rotation(value: object) -> int:
    try:
        rotation = int(round(float(value))) % 360
    except (TypeError, ValueError):
        return 0
    return min((0, 90, 180, 270), key=lambda candidate: abs(candidate - rotation))


def probe_video_geometry(path: Path) -> VideoGeometry:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to detect the video subtitle layout.")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe could not inspect the video dimensions: {path}")
    try:
        payload = json.loads(result.stdout)
        stream = payload["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
    except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError(f"ffprobe returned invalid video dimensions: {path}") from error
    rotation_value = (stream.get("tags") or {}).get("rotate")
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            rotation_value = side_data["rotation"]
            break
    rotation = _normalized_rotation(rotation_value)
    display_width, display_height = (height, width) if rotation in {90, 270} else (width, height)
    return VideoGeometry(width, height, rotation, display_width, display_height)


def media_stream_types(path: Path) -> set[str]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required to inspect ambiguous media containers such as WebM.")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"ffprobe could not inspect media streams: {path}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ffprobe returned invalid media metadata: {path}") from error
    return {
        str(stream.get("codec_type") or "").strip()
        for stream in payload.get("streams", [])
        if str(stream.get("codec_type") or "").strip()
    }


def is_supported_audio(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return True
    if suffix in AMBIGUOUS_SUFFIXES:
        stream_types = media_stream_types(path)
        return "audio" in stream_types and "video" not in stream_types
    return False


def is_supported_video(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        return False
    if suffix in AMBIGUOUS_SUFFIXES:
        return "video" in media_stream_types(path)
    return True
