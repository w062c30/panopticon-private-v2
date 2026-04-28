"""Load a ``.env`` file from the repo root into ``os.environ`` (stdlib only; no python-dotenv)."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def parse_dotenv_lines(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    return out


def load_repo_env(*, override: bool = False) -> int:
    """
    Load ``<repo>/.env`` if present. Returns number of keys applied.
    By default, existing ``os.environ`` entries win (override=False).
    """
    path = repo_root() / ".env"
    if not path.is_file():
        return 0
    try:
        data = parse_dotenv_lines(path.read_text(encoding="utf-8"))
    except OSError:
        return 0
    n = 0
    for k, v in data.items():
        if not k:
            continue
        if not override and os.environ.get(k):
            continue
        os.environ[k] = v
        n += 1
    return n
