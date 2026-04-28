#!/usr/bin/env python3
"""
Unify `.env` and `.env.example` safely.

Rules:
- `.env.example` defines the canonical key set (committable template).
- `.env` keeps local secret/runtime values (never committed).
- Missing keys from `.env.example` are added to `.env` with empty values.
- Unknown keys in `.env` are preserved.
"""

from __future__ import annotations

from pathlib import Path
import sys

# repo root
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from panopticon_py.load_env import parse_dotenv_lines  # noqa: E402


def main() -> int:
    env_example = ROOT / ".env.example"
    env = ROOT / ".env"
    if not env_example.is_file():
        print("Missing .env.example", file=sys.stderr)
        return 1

    example_map = parse_dotenv_lines(env_example.read_text(encoding="utf-8"))
    env_map = parse_dotenv_lines(env.read_text(encoding="utf-8")) if env.is_file() else {}

    merged = dict(env_map)
    added = 0
    for k in example_map.keys():
        # Treat blank values as missing so blank KEY= lines get properly filled
        if k not in merged or not merged[k].strip():
            merged[k] = ""
            added += 1

    lines = [
        "# Unified by scripts/unify_env.py\n",
        "# Local runtime file (gitignored). Keep secrets here.\n\n",
    ]
    for k in sorted(merged.keys()):
        v = merged[k]
        lines.append(f"{k}={v}\n")

    env.write_text("".join(lines), encoding="utf-8")
    print(f"Unified env files. Added {added} missing keys into .env")
    print("Canonical key schema: .env.example")
    print("Runtime values file: .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
