"""
process_guard.py — Singleton enforcement + version identity.

Flow on process start:
    1. acquire_singleton(name) — kills stale instance, writes PID file
    2. verify_version(name, MY_VERSION) — compares against versions_ref.json
    3. write process_manifest.json entry

Usage in every entry-point script:
    from panopticon_py.utils.process_guard import acquire_singleton
    PROCESS_VERSION = "v1.0.1-D52"
    acquire_singleton("radar", PROCESS_VERSION)

Inter-process version check:
    from panopticon_py.utils.process_guard import check_peer_version
    peer = check_peer_version("radar")
    if peer and not peer["version_match"]:
        logger.warning("Radar may be stale: %s", peer["version"])
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_RUN_DIR = _PROJECT_ROOT / "run"
_VERSIONS_REF = _RUN_DIR / "versions_ref.json"
_MANIFEST = _RUN_DIR / "process_manifest.json"
_MANIFEST_LOCK = _RUN_DIR / ".manifest.lock"

VALID_PROCESS_NAMES = frozenset({"radar", "orchestrator", "backend", "frontend", "analysis_worker"})


# ── PID file helpers ────────────────────────────────────────────────────────────

def _pid_path(name: str) -> Path:
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    return _RUN_DIR / f"{name}.pid"


def _is_alive(pid: int) -> bool:
    """Return True if PID is alive."""
    try:
        if sys.platform == "win32":
            import ctypes
            SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if not handle:
                return False
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
            return result == 258  # WAIT_TIMEOUT = still alive
        else:
            os.kill(pid, 0)
            return True
    except (OSError, PermissionError):
        return False


def _kill(pid: int, timeout: float = 5.0) -> bool:
    """Kill PID and return True if confirmed dead."""
    logger.warning("[guard] Killing stale PID=%d ...", pid)
    try:
        if sys.platform == "win32":
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            import signal as _sig
            os.kill(pid, _sig.SIGTERM)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not _is_alive(pid):
                    break
                time.sleep(0.25)
            if _is_alive(pid):
                os.kill(pid, _sig.SIGKILL)
    except (ProcessLookupError, PermissionError) as exc:
        logger.debug("[guard] Kill exception (likely already dead): %s", exc)

    time.sleep(0.5)
    dead = not _is_alive(pid)
    level = logging.INFO if dead else logging.ERROR
    logger.log(level, "[guard] PID=%d dead=%s", pid, dead)
    return dead


# ── Manifest helpers ────────────────────────────────────────────────────────────

def _read_manifest() -> dict:
    try:
        if _MANIFEST.exists():
            return json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _write_manifest(name: str, entry: dict) -> None:
    """Atomic write to process_manifest.json using a simple lock file."""
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 3.0
    while _MANIFEST_LOCK.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    _MANIFEST_LOCK.touch()
    try:
        manifest = _read_manifest()
        manifest[name] = entry
        tmp = _MANIFEST.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(_MANIFEST)
        logger.info("[guard] _write_manifest: wrote %s to manifest, total keys=%d", name, len(manifest))
    except Exception as exc:
        logger.error("[guard] _write_manifest: failed to write %s: %s", name, exc)
        raise
    finally:
        try:
            _MANIFEST_LOCK.unlink()
        except OSError:
            pass


def _clear_manifest_entry(name: str) -> None:
    manifest = _read_manifest()
    if name in manifest:
        manifest[name]["status"] = "stopped"
        manifest[name]["stop_time"] = datetime.now(timezone.utc).isoformat()
        tmp = _MANIFEST.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        tmp.replace(_MANIFEST)


# ── Version helpers ────────────────────────────────────────────────────────────

def _read_expected_version(name: str) -> Optional[str]:
    try:
        ref = json.loads(_VERSIONS_REF.read_text(encoding="utf-8"))
        return ref.get(name)
    except (json.JSONDecodeError, OSError):
        logger.warning("[guard] versions_ref.json missing or corrupt — skipping version check")
        return None


def _version_matches(actual: str, expected: str) -> bool:
    """Compare base version strings (strip D-suffix)."""
    def _base(v: str) -> str:
        return v.rsplit("-", 1)[0] if v else ""
    return _base(actual) == _base(expected)


# ── Public API ───────────────────────────────────────────────────────────────

def acquire_singleton(name: str, version: str = "v0.0.0-D0") -> None:
    """
    Enforce singleton for this process name.

    1. Validate name against VALID_PROCESS_NAMES
    2. Kill any existing stale instance (via PID file)
    3. Write PID lock file
    4. Compare MY_VERSION against versions_ref.json (log CRITICAL if mismatch)
    5. Write entry to process_manifest.json
    6. Register atexit cleanup

    MUST be called as FIRST executable line after imports in every entry point.
    """
    if name not in VALID_PROCESS_NAMES:
        raise ValueError(
            f"[guard] Unknown process name '{name}'. "
            f"Valid: {sorted(VALID_PROCESS_NAMES)}. "
            f"Add to VALID_PROCESS_NAMES in process_guard.py if adding a new process."
        )

    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    pid_path = _pid_path(name)

    # Step 1: Kill stale instance
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
        except (ValueError, OSError):
            old_pid = None

        if old_pid and old_pid != current_pid and _is_alive(old_pid):
            logger.warning(
                "[guard] %s: stale instance PID=%d found. Killing before start.",
                name, old_pid
            )
            killed = _kill(old_pid)
            if not killed:
                logger.error(
                    "[guard] CRITICAL: Could not kill stale %s PID=%d. "
                    "Duplicate may still be running. Proceeding anyway.",
                    name, old_pid
                )

    # Step 2: Write PID file
    pid_path.write_text(str(current_pid))
    logger.info(
        "[guard] %s singleton acquired (PID=%d, version=%s)",
        name, current_pid, version
    )

    # Step 3: Version check
    expected = _read_expected_version(name)
    version_match = True
    if expected:
        version_match = _version_matches(version, expected)
        if not version_match:
            logger.critical(
                "[guard] VERSION MISMATCH — %s is running %s but versions_ref.json "
                "expects %s. You may be running STALE CODE. "
                "Agent must update versions_ref.json after every code change.",
                name, version, expected
            )
        else:
            logger.info("[guard] %s version OK: %s == %s", name, version, expected)
    else:
        logger.warning("[guard] %s: no expected version in versions_ref.json", name)

    # Step 4: Write process manifest
    _write_manifest(name, {
        "pid": current_pid,
        "version": version,
        "expected": expected,
        "version_match": version_match,
        "start_time": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "status": "running",
    })

    # Step 5: Register atexit cleanup
    def _release() -> None:
        try:
            if pid_path.exists() and pid_path.read_text().strip() == str(current_pid):
                pid_path.unlink()
        except OSError:
            pass
        try:
            _clear_manifest_entry(name)
        except Exception:
            pass
        logger.info("[guard] %s released singleton (PID=%d)", name, current_pid)

    atexit.register(_release)


def update_heartbeat(name: str) -> None:
    """
    Update the last_heartbeat_ts field in process_manifest.json for the given process.
    Called periodically by each process to indicate liveness beyond start_time.
    """
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 3.0
    while _MANIFEST_LOCK.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    _MANIFEST_LOCK.touch()
    try:
        manifest = _read_manifest()
        if name in manifest:
            manifest[name]["last_heartbeat_ts"] = datetime.now(timezone.utc).isoformat()
            tmp = _MANIFEST.with_suffix(".tmp")
            tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            tmp.replace(_MANIFEST)
    except Exception as exc:
        logger.error("[guard] update_heartbeat: failed to update %s: %s", name, exc)
    finally:
        try:
            _MANIFEST_LOCK.unlink()
        except OSError:
            pass


def check_peer_version(name: str, required_base: Optional[str] = None) -> Optional[dict]:
    """
    Check if a peer process is running the expected version.
    Reads process_manifest.json (runtime state).

    Returns manifest entry dict, or None if process not in manifest.
    Logs WARNING if version_match=False or process not found.

    Does NOT block or raise — version mismatch is warn-only.
    Architect decides on escalation.

    Usage:
        peer = check_peer_version("radar")
        if peer and not peer["version_match"]:
            logger.warning("Radar may be stale: %s", peer["version"])
    """
    manifest = _read_manifest()
    entry = manifest.get(name)

    if not entry:
        logger.warning(
            "[guard] check_peer_version: '%s' not found in process_manifest.json",
            name
        )
        return None

    if entry.get("status") != "running":
        logger.warning(
            "[guard] check_peer_version: '%s' status=%s",
            name, entry.get("status")
        )

    if not entry.get("version_match", True):
        logger.warning(
            "[guard] PEER VERSION MISMATCH: %s running %s, expected %s",
            name, entry.get("version"), entry.get("expected")
        )

    if required_base:
        actual_base = entry.get("version", "").rsplit("-", 1)[0]
        if actual_base != required_base:
            logger.warning(
                "[guard] PEER VERSION REQUIREMENT UNMET: need %s/%s, got %s",
                name, required_base, entry.get("version")
            )

    return entry


def get_all_versions() -> dict:
    """Return full process_manifest.json as dict. Used by backend /api/versions endpoint."""
    return _read_manifest()
