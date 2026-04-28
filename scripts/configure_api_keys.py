#!/usr/bin/env python3
"""
Interactive helper: write API keys and optional URLs into a repo-root ``.env`` file.

By default, secret prompts use visible ``input()`` so paste works in most terminals.

``--hidden`` uses ``getpass`` on Unix; on **Windows** it uses ``msvcrt`` (masked ``*``),
because ``getpass`` often ignores keystrokes in VS Code / PowerShell. If input still
fails, use ``--import-env path/to/local.snippet`` (paste keys in an editor, then merge).

Run from repo root:  python scripts/configure_api_keys.py
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

# Ensure repo root on path when run as ``python scripts/configure_api_keys.py``
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from panopticon_py.load_env import parse_dotenv_lines  # noqa: E402


def _env_path() -> Path:
    return _ROOT / ".env"


def _mask(s: str, keep: int = 4) -> str:
    if not s:
        return "(empty)"
    if len(s) <= keep:
        return "***"
    return s[:2] + "…" + s[-2:] + f" ({len(s)} chars)"


def _read_hidden_line_msvcrt(prompt: str) -> str:
    """Windows: read a line with ``*`` masking (paste works; getpass often does not)."""
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in "\r\n":
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x08":  # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch in ("\x00", "\xe0"):  # Arrow / function key prefix
            msvcrt.getwch()
            continue
        buf.append(ch)
        sys.stdout.write("*")
        sys.stdout.flush()
    return "".join(buf).strip()


def _read_hidden_line(prompt: str) -> str:
    if sys.platform == "win32":
        return _read_hidden_line_msvcrt(prompt)
    return getpass.getpass(prompt).strip()


def _prompt_secret(name: str, current: str | None, *, hidden: bool) -> str | None:
    hint = f" [currently {_mask(current or '')}]" if current else ""
    print(f"\n{name}{hint}")
    print("  Press Enter to leave unchanged.")
    if hidden:
        entered = _read_hidden_line(f"  {name}: ")
    else:
        entered = input(f"  {name}: ").strip()
    return entered if entered else None


def _prompt_plain(name: str, current: str | None) -> str | None:
    hint = f" [current: {current!r}]" if current else ""
    print(f"\n{name}{hint}")
    print("  Press Enter to leave unchanged.")
    entered = input(f"  {name}: ").strip()
    return entered if entered else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Write Panopticon secrets to repo-root .env")
    ap.add_argument(
        "--hidden",
        action="store_true",
        help="Masked secret input (Windows: msvcrt + *; Unix: getpass).",
    )
    ap.add_argument(
        "--import-env",
        metavar="FILE",
        help="Merge KEY=value lines from FILE into .env (paste-friendly); then prompts can override.",
    )
    ap.add_argument(
        "--no-prompt",
        action="store_true",
        help="With --import-env only: write .env and exit (no questions).",
    )
    args = ap.parse_args()
    hidden = bool(args.hidden)

    path = _env_path()
    existing: dict[str, str] = {}
    if path.is_file():
        try:
            existing = parse_dotenv_lines(path.read_text(encoding="utf-8"))
        except OSError as e:
            print(f"Could not read {path}: {e}", file=sys.stderr)
            return 1

    print("Panopticon — configure secrets for `.env` (file is gitignored).")
    print(f"Target: {path}")
    if args.no_prompt and not args.import_env:
        print("--no-prompt requires --import-env.", file=sys.stderr)
        return 2

    updates: dict[str, str] = dict(existing)

    if args.import_env:
        snippet = Path(args.import_env)
        try:
            imported = parse_dotenv_lines(snippet.read_text(encoding="utf-8"))
        except OSError as e:
            print(f"Could not read {snippet}: {e}", file=sys.stderr)
            return 1
        updates.update(imported)
        print(f"Merged {len(imported)} entries from {snippet}", file=sys.stderr)

    if not hidden and not args.no_prompt:
        print(
            "\nNote: API key prompts echo characters (paste-friendly). "
            "Use a private screen, or --hidden for masked input, or --import-env FILE.\n",
            file=sys.stderr,
        )
    elif hidden and sys.platform == "win32" and not args.no_prompt:
        print(
            "\nMasked input (Windows): characters show as *. Paste (Ctrl+V) should work. "
            "If not, omit --hidden or use: python scripts/configure_api_keys.py --import-env your.snippet --no-prompt\n",
            file=sys.stderr,
        )

    if args.no_prompt:
        # Skip interactive prompts; write merged .env only
        pass
    else:

        for key in ("MORALIS_API_KEY", "NVIDIA_API_KEY"):
            new = _prompt_secret(key, updates.get(key), hidden=hidden)
            if new is not None:
                updates[key] = new

        for key in ("POLYGON_RPC_URL",):
            new = _prompt_plain(key, updates.get(key))
            if new is not None:
                updates[key] = new

        for key in ("PANOPTICON_CLOB_TOKEN_ID", "WATCH_WALLET_LIST"):
            new = _prompt_plain(key, updates.get(key))
            if new is not None:
                updates[key] = new

    # Drop empty values so we do not wipe optional keys with blanks
    cleaned = {k: v for k, v in updates.items() if v.strip()}

    def _escape_double(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def _format_line(k: str, v: str) -> str:
        if "\n" in v:
            v = v.replace("\n", "\\n")
        safe = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+:/@=')
        if v and all(c in safe for c in v):
            return f"{k}={v}\n"
        return f'{k}="{_escape_double(v)}"\n'

    header = (
        "# Generated/edited by scripts/configure_api_keys.py\n"
        "# Do not commit this file. See .env.example for variable names.\n\n"
    )
    lines = [header]
    for k in sorted(cleaned.keys()):
        lines.append(_format_line(k, cleaned[k]))

    try:
        path.write_text("".join(lines), encoding="utf-8")
    except OSError as e:
        print(f"Could not write {path}: {e}", file=sys.stderr)
        return 1

    print(f"\nWrote {len(cleaned)} entries to {path}")
    print("Python entrypoints load this file automatically via panopticon_py.load_env.load_repo_env().")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
