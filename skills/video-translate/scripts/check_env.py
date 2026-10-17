#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from common import (
    CREDENTIAL_KEYS,
    describe_env_search,
    prepare_env_file,
)
from setup_state import mark_initialized


# The synchronous path never uploads through OkFile, so its token is only
# needed by the explicit filetrans fallback.
REQUIRED_ENV = tuple(key for key in CREDENTIAL_KEYS if key != "OKFILE_TOKEN")
OPTIONAL_ENV = tuple(key for key in CREDENTIAL_KEYS if key == "OKFILE_TOKEN")

ENV_SETUP_HINTS = {
    "DASHSCOPE_API_KEY": (
        "Sign in to Alibaba Model Studio at https://bailian.console.aliyun.com/, create a DashScope "
        "API key, and add a small balance (2-10 CNY) before batch use."
    ),
    "ALIYUN_WORKSPACE_ID": (
        "Copy the workspace ID from the Alibaba Model Studio console (https://bailian.console.aliyun.com/)."
    ),
    "OKFILE_TOKEN": (
        "Optional: only the filetrans fallback uploads through OkFile. Register at "
        "https://www.okfile.com/ and create/copy the API key from "
        "https://www.okfile.com/en/account/api-keys."
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the video subtitle workflow environment.")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Explicit local .env path. Omit to discover one: $ALIYUN_ENV_FILE, then the nearest "
            ".env at or above the Skill directory and the current directory that declares a "
            "provider credential key."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON result for automation runners.")
    parser.add_argument(
        "--no-mark-initialized",
        action="store_true",
        help="Do not record successful first-use initialization for this user environment.",
    )
    return parser.parse_args()


def has_real_value(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return stripped != "..." and not stripped.startswith("填写")


def check_python(errors: list[str]) -> None:
    version = sys.version_info
    print(f"Python: {platform.python_version()} ({sys.executable})")
    if version < (3, 9):
        errors.append("[VTZ-E001] Python 3.9+ is required.")


def check_ffmpeg_tools(errors: list[str]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = ", ".join(name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path)
        errors.append(f"[VTZ-E002] {missing} was not found on PATH.")
        system = platform.system().lower()
        if system == "windows":
            print("Install hint: winget install Gyan.FFmpeg")
        elif system == "darwin":
            print("Install hint: brew install ffmpeg")
        else:
            print("Install hint: use your package manager, for example apt install ffmpeg")
        return

    for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        result = subprocess.run([path, "-version"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            print(f"{name}: available at {path}")
        else:
            errors.append(f"[VTZ-E002] {name} did not run successfully.")


def check_env_values(errors: list[str]) -> None:
    for key in REQUIRED_ENV:
        if has_real_value(os.environ.get(key)):
            print(f"{key}: set")
        else:
            hint = ENV_SETUP_HINTS.get(key, "")
            message = f"[VTZ-E001] {key} is missing or still a placeholder."
            if hint:
                message = f"{message} {hint}"
            errors.append(message)

    for key in OPTIONAL_ENV:
        if has_real_value(os.environ.get(key)):
            print(f"{key}: set")
        else:
            print(f"{key}: not set (optional; only the filetrans fallback uploads through OkFile)")

    region = os.environ.get("ALIYUN_REGION") or "cn-beijing"
    print(f"ALIYUN_REGION: {region}")


def main() -> int:
    args = parse_args()
    search = prepare_env_file(args.env_file, start=Path(__file__).resolve().parent, report=False)

    errors: list[str] = []
    print(f"OS: {platform.system()} {platform.release()} ({platform.machine()})")
    if search.path is not None:
        print(describe_env_search(search))
    elif search.explicit:
        errors.append(f"[VTZ-E001] {describe_env_search(search)}")
        print(describe_env_search(search), file=sys.stderr)
    else:
        print(describe_env_search(search))
    check_python(errors)
    check_ffmpeg_tools(errors)
    check_env_values(errors)
    state_path = None
    if not errors and not args.no_mark_initialized:
        state_path = mark_initialized()
    if args.json:
        result = {
            "ok": not errors,
            "python": platform.python_version(),
            "ffmpeg": bool(shutil.which("ffmpeg")),
            "ffprobe": bool(shutil.which("ffprobe")),
            "env": {key: has_real_value(os.environ.get(key)) for key in CREDENTIAL_KEYS},
            "required_env": list(REQUIRED_ENV),
            "optional_env": list(OPTIONAL_ENV),
            "env_file": str(search.path) if search.path is not None else "",
            "env_file_explicit": search.explicit,
            "env_file_candidates": [str(candidate) for candidate in search.candidates],
            "errors": errors,
            "initialization_state": str(state_path) if state_path else "",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    if errors:
        print("\nEnvironment check failed:")
        for error in errors:
            print(f"- {error}")
        print("\nCreate/update the local .env file only after reviewing the required provider credentials in SKILL.md.")
        return 1

    print("\nEnvironment check passed.")
    if state_path:
        print(f"First-use initialization recorded: {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
