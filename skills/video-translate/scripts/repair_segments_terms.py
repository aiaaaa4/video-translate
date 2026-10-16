#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DEFAULT_RULES = Path(__file__).resolve().parents[1] / "references" / "term_repair_rules.json"
FIELD_RE = re.compile(r"^(SRC_DISPLAY|ZH):\s*(.*)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair domain terminology and proper names in segment display/translation fields."
    )
    parser.add_argument("segments", type=Path, help="Input segments.txt.")
    parser.add_argument("--out", type=Path, default=None, help="Output path. Defaults to overwriting input.")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    return parser.parse_args()


def load_rules(path: Path) -> dict[str, list[dict[str, str]]]:
    return json.loads(path.read_text(encoding="utf-8"))


def repair_value(value: str, rules: list[dict[str, str]]) -> tuple[str, int]:
    changes = 0
    repaired = value
    for rule in rules:
        pattern = rule["pattern"]
        replacement = rule["replacement"]
        repaired, count = re.subn(pattern, replacement, repaired)
        changes += count
    return repaired, changes


def main() -> int:
    args = parse_args()
    rules = load_rules(args.rules)
    field_rules = {
        "SRC_DISPLAY": rules.get("src_display", []),
        "ZH": rules.get("zh", []),
    }

    total_changes = 0
    output_lines: list[str] = []
    for line in args.segments.read_text(encoding="utf-8").splitlines():
        match = FIELD_RE.match(line)
        if not match:
            output_lines.append(line)
            continue

        field, value = match.groups()
        repaired, changes = repair_value(value, field_rules.get(field, []))
        total_changes += changes
        output_lines.append(f"{field}: {repaired}")

    out = args.out or args.segments
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    print(f"Applied {total_changes} repairs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
