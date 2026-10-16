#!/usr/bin/env python3
"""Export and re-create Alibaba Model Studio ASR hotword vocabularies on a new model.

Why this tool exists
--------------------
Alibaba ``speech-biasing`` vocabularies are bound to one ``target_model`` at
creation time, and ``update_vocabulary`` takes no ``target_model`` parameter.
A list compiled for ``fun-asr`` can therefore never be rebound to
``qwen-audio-3.1-asr-flash-filetrans``.  The two documented escapes are:

1. export the old list (``query_vocabulary``) and re-create it with the new
   ``target_model`` (``create_vocabulary``) -- what this tool automates; or
2. convert the hotwords into inline ("instant") hotwords passed directly in the
   transcription request as ``parameters.vocabulary = {"<text>": <weight>}``,
   which only the Qwen-Audio-3.x-ASR-Flash family accepts and which carries no
   model binding at all.  ``--export-json`` emits that inline map as well.

Contract (Alibaba, Beijing)
---------------------------
Endpoint: ``POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/customization``
Headers:  ``Authorization: Bearer <DASHSCOPE_API_KEY>``, ``Content-Type: application/json``
Body:     ``{"model": "speech-biasing", "input": {"action": ...}}`` -- ``model`` is
          always the fixed string ``speech-biasing``; the real model choice lives
          in ``input.target_model`` and only on ``create_vocabulary``.

Safety
------
* Dry run is the default.  Nothing is sent -- and no remote state changes --
  until ``--apply`` is passed; dry run prints the exact JSON payloads that would
  be sent.
* ``--export-json`` is a local write that must be requested explicitly.  It runs
  whenever the hotwords are available locally (from ``--hotwords-json``, or from
  a ``--source-vocabulary-id`` query that only happens under ``--apply``).  A
  ``--hotwords-json`` to ``--export-json`` conversion is fully offline: it needs
  no workspace ID and no API key.
* The ``DASHSCOPE_API_KEY`` value is read from the environment (after a minimal
  ``.env`` load) and is used only inside the ``Authorization`` header.  It is
  never printed, logged, summarized, or included in any payload.
* ``--delete`` and ``--update`` need ``--apply`` to affect the account.

Validation and normalization policy
-----------------------------------
Applied on export and again before ``create_vocabulary`` / ``update_vocabulary``:

* Hotword text must be a non-empty string; surrounding whitespace is stripped.
* Non-ASCII text: at most 15 characters counted over the whole text, including
  its ASCII characters.
* Pure-ASCII text: at most 7 space-separated fragments.
* Weight must be an integer: 1-5 (normal, recommended 4) or 50 (super hotword).
* At most 50 super hotwords and at most 2000 hotwords per list/request; both
  caps apply to the normalized list that would actually be sent.
* Duplicates are merged case-insensitively (``str.casefold``).  The first
  spelling and ``lang`` win, and the duplicate keeps the *highest* weight seen,
  so a super-hotword duplicate is never demoted.  Merging is never used to
  silently drop an entry: the caps are checked after merging and any violation
  raises.
* ``lang`` is optional.  Fun-ASR only honors ``zh``, ``en`` and ``ja``, and a
  ``lang`` only takes effect when it matches the request's ``language_hints``;
  other well-formed language tags are passed through rather than rejected, so a
  list shared with another model family still exports.

Environment
-----------
``--env-file`` (default ``.env``) is loaded into the environment with ``setdefault``
semantics, so a real environment variable always wins.  When the corresponding
flag is absent, these fallbacks apply:

* ``--workspace-id``  <- ``ALIYUN_WORKSPACE_ID`` (required for any remote action)
* ``--region``        <- ``ALIYUN_REGION``; the effective default is ``cn-beijing``
* ``--source-vocabulary-id`` <- ``ALIYUN_ASR_VOCABULARY_ID``
* ``--target-model``  <- ``ALIYUN_ASR_TARGET_MODEL``; the effective default is
  ``qwen-audio-3.1-asr-flash-filetrans``
* ``DASHSCOPE_API_KEY`` is used only for the ``Authorization`` header.

Export format (``--export-json``)
---------------------------------
``{"target_model": str, "vocabulary": [{"text": str, "weight": int, "lang": str}],
"inline": {"<text>": int}}`` -- ``inline`` is exactly the map form the Skill can
feed into ``parameters.vocabulary``.

Examples
--------
Dry run (default) of a full migration::

    python3 tools/migrate_asr_hotwords.py --workspace-id ws-xxxx --list
    python3 tools/migrate_asr_hotwords.py --workspace-id ws-xxxx \\
        --source-vocabulary-id voc-old --export-json hotwords.json \\
        --create-new --prefix vt01

Perform it, then delete the old, now-unbound list::

    python3 tools/migrate_asr_hotwords.py --workspace-id ws-xxxx \\
        --source-vocabulary-id voc-old --export-json hotwords.json \\
        --create-new --prefix vt01 --apply
    python3 tools/migrate_asr_hotwords.py --workspace-id ws-xxxx --delete voc-old --apply

Re-create a list from an already-exported file (no source list needed)::

    python3 tools/migrate_asr_hotwords.py --workspace-id ws-xxxx \\
        --hotwords-json hotwords.json --create-new --prefix vt02 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_REGION = "cn-beijing"
DEFAULT_TARGET_MODEL = "qwen-audio-3.1-asr-flash-filetrans"
DEFAULT_TARGET_MODEL_ENV = "ALIYUN_ASR_TARGET_MODEL"
API_KEY_ENV = "DASHSCOPE_API_KEY"
WORKSPACE_ID_ENV = "ALIYUN_WORKSPACE_ID"
REGION_ENV = "ALIYUN_REGION"
VOCABULARY_ID_ENV = "ALIYUN_ASR_VOCABULARY_ID"

# `model` is always this fixed string; the ASR model lives in input.target_model.
BIASING_MODEL = "speech-biasing"
CUSTOMIZATION_PATH = "/api/v1/services/audio/asr/customization"

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$", re.IGNORECASE)
PREFIX_RE = re.compile(r"^[a-z0-9]{1,10}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

MAX_HOTWORDS = 2000
MAX_SUPER_HOTWORDS = 50
SUPER_HOTWORD_WEIGHT = 50
NORMAL_WEIGHTS = (1, 2, 3, 4, 5)
ALLOWED_WEIGHTS = frozenset({*NORMAL_WEIGHTS, SUPER_HOTWORD_WEIGHT})
RECOMMENDED_WEIGHT = 4
MAX_NON_ASCII_CHARS = 15
MAX_ASCII_FRAGMENTS = 7

# Injectable HTTP poster: poster(url, api_key, body) -> decoded JSON object.
Poster = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class MigrationError(RuntimeError):
    """Bad input, bad configuration, or an invalid vocabulary (exit code 2)."""

    exit_code = 2


class RemoteError(MigrationError):
    """A remote call or remote response failed (exit code 1)."""

    exit_code = 1


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #


def load_env(path: Path) -> None:
    """Load a minimal ``KEY=VALUE`` file into the environment without echoing values."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def has_real_secret(value: str | None) -> bool:
    if not value:
        return False
    stripped = value.strip()
    return stripped != "..." and not stripped.startswith("填写")


def require_api_key(env_file: Path) -> str:
    """Return the API key for the Authorization header only; its value is never printed."""
    load_env(env_file)
    value = os.environ.get(API_KEY_ENV)
    if not has_real_secret(value):
        raise MigrationError(
            f"Missing {API_KEY_ENV}. Set it in {env_file} or the environment; "
            "--apply needs it, --dry-run does not."
        )
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


def customization_base_url(workspace_id: str, region: str) -> str:
    if not workspace_id or not IDENTIFIER_RE.fullmatch(workspace_id):
        raise MigrationError(
            "Alibaba workspace ID must contain only letters, digits, and hyphens; "
            "pass --workspace-id or set ALIYUN_WORKSPACE_ID."
        )
    if not region or not IDENTIFIER_RE.fullmatch(region):
        raise MigrationError("--region must be a region identifier such as cn-beijing.")
    return f"https://{workspace_id}.{region}.maas.aliyuncs.com"


def customization_url(base_url: str) -> str:
    return f"{base_url}{CUSTOMIZATION_PATH}"


# --------------------------------------------------------------------------- #
# Vocabulary validation and normalization
# --------------------------------------------------------------------------- #


def contains_non_ascii(text: str) -> bool:
    return any(ord(char) > 0x7F for char in text)


def hotword_text_problem(text: str) -> str | None:
    """Return a human-readable rule violation for one hotword text, or ``None``."""
    if contains_non_ascii(text):
        if len(text) > MAX_NON_ASCII_CHARS:
            return (
                f"non-ASCII hotword must be at most {MAX_NON_ASCII_CHARS} characters counted over "
                f"the whole text including its ASCII characters; got {len(text)}"
            )
        return None
    fragments = text.split()
    if len(fragments) > MAX_ASCII_FRAGMENTS:
        return (
            f"pure-ASCII hotword must be at most {MAX_ASCII_FRAGMENTS} space-separated "
            f"fragments; got {len(fragments)}"
        )
    return None


def coerce_weight(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MigrationError(f"hotword weight must be an integer, got {value!r}")
    if isinstance(value, float):
        if not value.is_integer():
            raise MigrationError(f"hotword weight must be an integer, got {value!r}")
        value = int(value)
    weight = int(value)
    if weight not in ALLOWED_WEIGHTS:
        raise MigrationError(
            f"hotword weight must be 1-{max(NORMAL_WEIGHTS)} or {SUPER_HOTWORD_WEIGHT} "
            f"(super hotword), got {weight}"
        )
    return weight


def _entry_weight(index: int, text: str, raw_weight: object) -> int:
    """Validate one entry's weight while naming that entry in the error."""
    try:
        return coerce_weight(raw_weight)
    except MigrationError as exc:
        raise MigrationError(f"hotword #{index} {text!r} is invalid: {exc}") from exc


def normalize_vocabulary(entries: Iterable[object]) -> list[dict[str, Any]]:
    """Validate, canonicalize, and case-insensitively deduplicate hotword entries.

    Validation errors name the offending entry.  Duplicates keep the first
    spelling and ``lang`` with the highest weight seen for that spelling.  The
    limits are enforced on the deduplicated list, which is what would be sent.
    """
    normalized: list[dict[str, Any]] = []
    positions: dict[str, int] = {}

    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, Mapping):
            raise MigrationError(
                f"hotword #{index} must be an object with text/weight/lang, "
                f"got {type(raw_entry).__name__}"
            )

        raw_text = raw_entry.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise MigrationError(f"hotword #{index} has an empty or non-string text: {raw_text!r}")
        text = raw_text.strip()
        problem = hotword_text_problem(text)
        if problem is not None:
            raise MigrationError(f"hotword #{index} {text!r} is invalid: {problem}")

        weight = _entry_weight(index, text, raw_entry.get("weight"))

        raw_lang = raw_entry.get("lang")
        if raw_lang is None:
            raw_lang = ""
        if not isinstance(raw_lang, str):
            raise MigrationError(f"hotword #{index} {text!r} has a non-string lang: {raw_lang!r}")
        lang = raw_lang.strip()
        if lang and not LANG_RE.fullmatch(lang):
            raise MigrationError(
                f"hotword #{index} {text!r} has an invalid lang {lang!r}; "
                "use a language tag such as zh, en, or ja"
            )

        key = text.casefold()
        existing = positions.get(key)
        if existing is None:
            positions[key] = len(normalized)
            normalized.append({"text": text, "weight": weight, "lang": lang})
        elif weight > int(normalized[existing]["weight"]):
            normalized[existing]["weight"] = weight

    if len(normalized) > MAX_HOTWORDS:
        raise MigrationError(
            f"{len(normalized)} unique hotwords exceed the {MAX_HOTWORDS} hotword limit per list"
        )
    super_count = sum(1 for entry in normalized if entry["weight"] == SUPER_HOTWORD_WEIGHT)
    if super_count > MAX_SUPER_HOTWORDS:
        raise MigrationError(
            f"{super_count} super hotwords (weight {SUPER_HOTWORD_WEIGHT}) exceed the "
            f"{MAX_SUPER_HOTWORDS} super-hotword limit per list"
        )
    return normalized


def api_vocabulary_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": str(entry["text"]), "weight": int(entry["weight"])}
    lang = str(entry.get("lang") or "")
    if lang:
        payload["lang"] = lang
    return payload


def _api_vocabulary(vocabulary: Iterable[object]) -> list[dict[str, Any]]:
    return [api_vocabulary_entry(entry) for entry in normalize_vocabulary(vocabulary)]


def validate_prefix(prefix: str | None) -> str:
    value = (prefix or "").strip()
    if not PREFIX_RE.fullmatch(value):
        raise MigrationError(
            f"invalid prefix {prefix!r}: use 1-10 characters of lowercase letters or digits"
        )
    return value


def _require_vocabulary_id(vocabulary_id: str | None) -> str:
    value = (vocabulary_id or "").strip()
    if not value or any(char.isspace() for char in value):
        raise MigrationError(f"invalid vocabulary_id: {vocabulary_id!r}")
    return value


# --------------------------------------------------------------------------- #
# Request bodies -- one builder per action, all carrying the fixed biasing model
# --------------------------------------------------------------------------- #


def create_vocabulary_body(
    *, target_model: str, prefix: str, vocabulary: Iterable[object]
) -> dict[str, Any]:
    """Build ``create_vocabulary``; this is the only action that sets target_model."""
    return {
        "model": BIASING_MODEL,
        "input": {
            "action": "create_vocabulary",
            "target_model": target_model,
            "prefix": validate_prefix(prefix),
            "vocabulary": _api_vocabulary(vocabulary),
        },
    }


def list_vocabulary_body(
    *,
    prefix: str | None = None,
    page_index: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "list_vocabulary"}
    if prefix is not None:
        payload["prefix"] = prefix
    if page_index is not None:
        payload["page_index"] = page_index
    if page_size is not None:
        payload["page_size"] = page_size
    return {"model": BIASING_MODEL, "input": payload}


def query_vocabulary_body(vocabulary_id: str) -> dict[str, Any]:
    return {
        "model": BIASING_MODEL,
        "input": {
            "action": "query_vocabulary",
            "vocabulary_id": _require_vocabulary_id(vocabulary_id),
        },
    }


def update_vocabulary_body(*, vocabulary_id: str, vocabulary: Iterable[object]) -> dict[str, Any]:
    """Build ``update_vocabulary``; deliberately carries no ``target_model``."""
    return {
        "model": BIASING_MODEL,
        "input": {
            "action": "update_vocabulary",
            "vocabulary_id": _require_vocabulary_id(vocabulary_id),
            "vocabulary": _api_vocabulary(vocabulary),
        },
    }


def delete_vocabulary_body(vocabulary_id: str) -> dict[str, Any]:
    return {
        "model": BIASING_MODEL,
        "input": {
            "action": "delete_vocabulary",
            "vocabulary_id": _require_vocabulary_id(vocabulary_id),
        },
    }


# --------------------------------------------------------------------------- #
# Response parsing
# --------------------------------------------------------------------------- #


def response_output(response: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(response, Mapping):
        raise RemoteError(f"response is not a JSON object: {type(response).__name__}")
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise RemoteError(f"response is missing an output object; keys: {sorted(response)}")
    return output


def response_status(response: Mapping[str, Any]) -> str:
    return str(response_output(response).get("status") or "UNKNOWN")


def created_vocabulary_id(response: Mapping[str, Any]) -> str:
    vocabulary_id = response_output(response).get("vocabulary_id")
    if not isinstance(vocabulary_id, str) or not vocabulary_id.strip():
        raise RemoteError("create_vocabulary response is missing output.vocabulary_id")
    return vocabulary_id.strip()


def export_document(
    target_model: str, vocabulary: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Build the local export document, including the inline ``text -> weight`` map."""
    entries: list[dict[str, Any]] = []
    inline: dict[str, int] = {}
    for entry in vocabulary:
        text = str(entry["text"])
        weight = int(entry["weight"])
        entries.append({"text": text, "weight": weight, "lang": str(entry.get("lang") or "")})
        inline[text] = weight
    return {"target_model": target_model, "vocabulary": entries, "inline": inline}


def parse_query_response(response: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a ``query_vocabulary`` response into the export document."""
    output = response_output(response)
    target_model = output.get("target_model")
    if not isinstance(target_model, str) or not target_model.strip():
        raise RemoteError("query_vocabulary response is missing output.target_model")
    raw_vocabulary = output.get("vocabulary")
    if not isinstance(raw_vocabulary, list):
        raise RemoteError("query_vocabulary response is missing the output.vocabulary array")
    vocabulary = normalize_vocabulary(raw_vocabulary)
    return export_document(target_model.strip(), vocabulary)


def parse_list_response(response: Mapping[str, Any]) -> list[dict[str, str]]:
    output = response_output(response)
    rows = output.get("vocabulary_list")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise RemoteError("list_vocabulary response has a malformed output.vocabulary_list")
    parsed: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RemoteError("list_vocabulary response contains a non-object list entry")
        parsed.append(
            {
                "vocabulary_id": str(row.get("vocabulary_id") or ""),
                "gmt_modified": str(row.get("gmt_modified") or ""),
                "status": str(row.get("status") or ""),
            }
        )
    return parsed


def write_json_document(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def load_hotwords_file(path: Path) -> list[dict[str, Any]]:
    """Read a local export document (or a bare hotword array) and normalize it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MigrationError(f"cannot read --hotwords-json {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"--hotwords-json {path} is not valid JSON: {exc}") from exc

    if isinstance(raw, Mapping):
        entries = raw.get("vocabulary")
        if entries is None:
            entries = raw.get("hotwords")
        if not isinstance(entries, list):
            raise MigrationError(f"{path} must contain a vocabulary array or a bare hotword array")
    elif isinstance(raw, list):
        entries = raw
    else:
        raise MigrationError(f"{path} must contain a vocabulary array or a bare hotword array")
    return normalize_vocabulary(entries)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


def post_json(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Default poster.  This is the only place the API key value is ever used."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RemoteError(f"POST {url} returned HTTP {exc.code}: {detail}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RemoteError(f"POST {url} failed: {exc}") from exc

    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RemoteError(f"POST {url} returned undecodable JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RemoteError(f"POST {url} returned non-object JSON")
    return decoded


def call_customization(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    poster: Poster = post_json,
) -> dict[str, Any]:
    """Send one customization request through the injectable poster."""
    return poster(customization_url(base_url), api_key, payload)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--workspace-id", default=None, help="Alibaba Model Studio workspace ID.")
    parser.add_argument("--region", default=None, help=f"Alibaba region (default {DEFAULT_REGION}).")
    parser.add_argument(
        "--source-vocabulary-id",
        default=None,
        help="Existing vocabulary to export and re-create on --target-model.",
    )
    parser.add_argument(
        "--target-model",
        default=None,
        help=f"ASR model for a new list (default {DEFAULT_TARGET_MODEL}).",
    )
    parser.add_argument(
        "--export-json",
        type=Path,
        default=None,
        help="Write the normalized hotwords (vocabulary + inline map) to this local JSON file.",
    )
    parser.add_argument(
        "--hotwords-json",
        type=Path,
        default=None,
        help="Read hotwords from a local export document instead of a remote source list.",
    )
    parser.add_argument("--list", action="store_true", help="List the account's vocabularies.")
    parser.add_argument(
        "--create-new",
        action="store_true",
        help="Create a new vocabulary with --target-model (needs --prefix).",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Vocabulary prefix for --create-new (lowercase letters and digits, at most 10).",
    )
    parser.add_argument(
        "--list-prefix",
        default=None,
        help="Optional prefix filter for --list; omit to list every vocabulary.",
    )
    parser.add_argument(
        "--update",
        default=None,
        metavar="VOCABULARY_ID",
        help="Replace one list's contents; cannot change its target_model.",
    )
    parser.add_argument("--delete", default=None, metavar="VOCABULARY_ID", help="Delete one list.")
    parser.add_argument("--page-index", type=int, default=None, help="--list page index (from 0).")
    parser.add_argument("--page-size", type=int, default=None, help="--list page size.")
    safety = parser.add_mutually_exclusive_group()
    safety.add_argument(
        "--apply",
        action="store_true",
        help="Actually call the Alibaba API. Without it every action only prints its payload.",
    )
    safety.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly stay offline (the default).",
    )
    return parser.parse_args(argv)


def emit(message: str, out: TextIO) -> None:
    print(message, file=out)


def print_request(url: str, payload: Mapping[str, Any], *, mode: str, out: TextIO) -> None:
    emit(f"[{mode}] POST {url}", out)
    emit(f"[{mode}] Authorization: Bearer <redacted>", out)
    emit(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False), out)
    emit("", out)


def request_summary(payload: Mapping[str, Any]) -> str:
    data = payload.get("input") or {}
    parts = [f"action={data.get('action')}"]
    for key in ("target_model", "prefix", "vocabulary_id"):
        if data.get(key):
            parts.append(f"{key}={data[key]}")
    vocabulary = data.get("vocabulary")
    if isinstance(vocabulary, list):
        parts.append(f"hotwords={len(vocabulary)}")
    return " ".join(parts)


def run(
    args: argparse.Namespace,
    *,
    poster: Poster = post_json,
    out: TextIO = sys.stdout,
) -> int:
    try:
        return _run(args, poster=poster, out=out)
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code


def _run(args: argparse.Namespace, *, poster: Poster, out: TextIO) -> int:
    load_env(args.env_file)

    workspace_id = args.workspace_id or os.environ.get(WORKSPACE_ID_ENV) or ""
    region = args.region or os.environ.get(REGION_ENV) or DEFAULT_REGION
    target_model = args.target_model or os.environ.get(DEFAULT_TARGET_MODEL_ENV) or DEFAULT_TARGET_MODEL
    source_id = args.source_vocabulary_id or os.environ.get(VOCABULARY_ID_ENV) or None

    mode = "apply" if args.apply else "dry-run"

    local_vocabulary: list[dict[str, Any]] | None = None
    if args.hotwords_json is not None:
        local_vocabulary = load_hotwords_file(args.hotwords_json)

    if args.export_json is not None and not (source_id or local_vocabulary is not None):
        raise MigrationError(
            "--export-json needs --source-vocabulary-id (read from the account) "
            "or --hotwords-json (read from a local file)"
        )

    # A local hotwords file plus --export-json is a complete, fully offline action.
    needs_remote = any([args.list, source_id, args.create_new, args.update, args.delete])
    if not needs_remote and (args.export_json is None or local_vocabulary is None):
        raise MigrationError(
            "nothing to do: pass --list, --source-vocabulary-id, --create-new, --update, "
            "--delete, or --hotwords-json with --export-json"
        )

    if needs_remote:
        base_url = customization_base_url(workspace_id, region)
    else:
        base_url = ""
    url = customization_url(base_url) if base_url else "<no remote endpoint: offline conversion>"

    prefix: str | None = None
    if args.create_new:
        prefix = validate_prefix(args.prefix)
    elif args.prefix is not None:
        raise MigrationError(
            "--prefix only applies to --create-new; use --list-prefix to filter --list"
        )
    list_prefix = args.list_prefix
    if list_prefix is not None:
        list_prefix = validate_prefix(list_prefix)

    # The key is required only when a remote action is really about to run.
    api_key = require_api_key(args.env_file) if (args.apply and needs_remote) else ""
    if not args.apply:
        emit("[dry-run] no network calls and no remote changes; re-run with --apply to execute.", out)
        emit("", out)

    # 1) Optional read-only listing first, so a plan always starts from real state.
    if args.list:
        body = list_vocabulary_body(
            prefix=list_prefix, page_index=args.page_index, page_size=args.page_size
        )
        if args.apply:
            response = call_customization(base_url, api_key, body, poster=poster)
            rows = parse_list_response(response)
            emit(f"[apply] POST {url} ({request_summary(body)}) -> {len(rows)} list(s)", out)
            for row in rows:
                emit(
                    f"  {row['vocabulary_id']}  {row['gmt_modified']}  {row['status']}",
                    out,
                )
        else:
            print_request(url, body, mode=mode, out=out)

    vocabulary: list[dict[str, Any]] | None = local_vocabulary
    document: dict[str, Any] | None = (
        export_document(target_model, local_vocabulary)
        if local_vocabulary is not None
        else None
    )
    unresolved_note: str | None = None

    # 2) Export the source list (network only under --apply).
    if source_id:
        body = query_vocabulary_body(source_id)
        if args.apply:
            response = call_customization(base_url, api_key, body, poster=poster)
            document = parse_query_response(response)
            vocabulary = [dict(entry) for entry in document["vocabulary"]]
            emit(
                f"[apply] POST {url} ({request_summary(body)}) -> target_model="
                f"{document['target_model']} hotwords={len(vocabulary)} "
                f"status={response_status(response)}",
                out,
            )
        else:
            print_request(url, body, mode=mode, out=out)
            unresolved_note = (
                f"<vocabulary resolved from query_vocabulary {source_id} at --apply time>"
            )

    if args.export_json is not None:
        if document is None:
            emit(
                f"[{mode}] --export-json {args.export_json} skipped: the source list is only read "
                "under --apply; pass --hotwords-json for an offline export.",
                out,
            )
        else:
            write_json_document(args.export_json, document)
            emit(
                f"[{mode}] wrote {args.export_json} ({len(document['vocabulary'])} hotwords, "
                f"{len(document['inline'])} inline entries)",
                out,
            )

    # 3) Create the replacement list bound to the new target model.
    if args.create_new:
        if vocabulary is not None:
            body = create_vocabulary_body(
                target_model=target_model, prefix=str(prefix), vocabulary=vocabulary
            )
            if args.apply:
                response = call_customization(base_url, api_key, body, poster=poster)
                emit(
                    f"[apply] POST {url} ({request_summary(body)}) -> vocabulary_id="
                    f"{created_vocabulary_id(response)}",
                    out,
                )
            else:
                print_request(url, body, mode=mode, out=out)
        elif unresolved_note is not None:
            body = create_vocabulary_body(
                target_model=target_model, prefix=str(prefix), vocabulary=[]
            )
            body["input"]["vocabulary"] = unresolved_note
            print_request(url, body, mode=mode, out=out)
            emit(
                "[dry-run] the vocabulary above is resolved from the source list only when "
                "--apply runs.",
                out,
            )
        else:
            raise MigrationError(
                "--create-new needs --source-vocabulary-id or --hotwords-json for the hotword payload"
            )

    # 4) Optional in-place content refresh (never a model rebind).
    if args.update:
        if vocabulary is not None:
            body = update_vocabulary_body(vocabulary_id=args.update, vocabulary=vocabulary)
            if args.apply:
                response = call_customization(base_url, api_key, body, poster=poster)
                emit(
                    f"[apply] POST {url} ({request_summary(body)}) -> status="
                    f"{response_status(response)} (target_model is unchanged)",
                    out,
                )
            else:
                print_request(url, body, mode=mode, out=out)
                emit(
                    "[dry-run] update_vocabulary cannot change target_model; re-create the list "
                    "to move it to another model.",
                    out,
                )
        elif unresolved_note is not None:
            body = update_vocabulary_body(vocabulary_id=args.update, vocabulary=[])
            body["input"]["vocabulary"] = unresolved_note
            print_request(url, body, mode=mode, out=out)
        else:
            raise MigrationError(
                "--update needs --source-vocabulary-id or --hotwords-json for the hotword payload"
            )

    # 5) Optional deletion of the now-unbound source list.
    if args.delete:
        body = delete_vocabulary_body(args.delete)
        if args.apply:
            response = call_customization(base_url, api_key, body, poster=poster)
            emit(
                f"[apply] POST {url} ({request_summary(body)}) -> status={response_status(response)}",
                out,
            )
        else:
            print_request(url, body, mode=mode, out=out)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
