#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path


SCHEMA_VERSION = 1
SKILL_ID = "aiaaaa4.video-translate"


def default_state_path() -> Path:
    if platform.system().lower() == "windows":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    return base / "aiaaaa4" / "video-translate" / "setup.json"


def environment_id() -> dict[str, object]:
    return {
        "platform": platform.system(),
        "machine": platform.machine(),
        "python_executable": str(Path(sys.executable).resolve()),
        "python_major_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def initialized(path: Path | None = None) -> bool:
    state_path = path or default_state_path()
    if not state_path.is_file():
        return False
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("skill_id") == SKILL_ID
        and payload.get("environment") == environment_id()
        and payload.get("status") == "ready"
    )


def mark_initialized(path: Path | None = None) -> Path:
    state_path = path or default_state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "skill_id": SKILL_ID,
        "status": "ready",
        "environment": environment_id(),
    }
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        state_path.chmod(0o600)
    except OSError:
        pass
    return state_path
