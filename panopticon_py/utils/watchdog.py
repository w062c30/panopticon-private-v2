"""
watchdog.py — Process liveness monitor + auto-restart supervisor.

Monitors all processes listed in WATCHED_PROCESSES by reading
process_manifest.json every POLL_INTERVAL_SEC seconds.

Liveness criteria:
  1. PID file exists AND PID is alive (os.kill(pid, 0))
  2. last_heartbeat_ts within HEARTBEAT_STALE_SEC seconds

If either criterion fails → restart the process.

Usage:
    python -m panopticon_py.utils.watchdog          # foreground
    python -m panopticon_py.utils.watchdog --daemon  # background (Unix only)

Design constraints:
  - Watchdog itself does NOT register as a singleton (avoids kill-self loop)
  - Watchdog writes its own heartbeat to run/watchdog_heartbeat.json
  - Max 3 consecutive restart attempts per process within 5 min → circuit breaker
  - Minimum restart gap: 10s (prevents tight crash loop)
  - Does NOT use subprocess.Popen shell=True (security constraint)

D113: Created for process health monitoring and auto-restart.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from panopticon_py.utils.process_guard import (
    _MANIFEST,
    _RUN_DIR,
    is_process_alive,
    update_heartbeat,
)

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────────

# D139: Corrected parents[2] — watchdog.py at panopticon_py/utils/ →
# parents[0]=utils/, parents[1]=panopticon_py/, parents[2]=project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

WATCHED_PROCESSES: dict[str, dict] = {
    # D132: radar runs inside orchestrator as asyncio task (D79 architecture).
    # Watchdog monitors orchestrator as the process that hosts radar.
    # Radar itself does not have a standalone PID — monitoring orchestrator covers radar health.
    "orchestrator": {
        "cmd": [sys.executable, "run_hft_orchestrator.py"],
        "cwd": str(_PROJECT_ROOT),
        "log": "run/orchestrator.log",
        "heartbeat_stale_sec": 90,   # orchestrator writes HB every 5s; 90s = 18x margin
    },
    "backend": {
        # D113: uvicorn runs on port 8001 (not 8000 — matches restart_all.ps1)
        "cmd": [sys.executable, "-m", "uvicorn",
                "panopticon_py.api.app:app",
                "--host", "0.0.0.0", "--port", "8001"],
        "cwd": str(_PROJECT_ROOT),
        "log": "run/backend.log",
        "heartbeat_stale_sec": 60,  # backend writes HB every 30s; 60s = 2x margin
    },
    "arb_scanner": {
        # D138: arb_scanner runs as standalone process with 30s fixed heartbeat
        "cmd": [sys.executable, "-m", "panopticon_py.execution.arb_scanner"],
        "cwd": str(_PROJECT_ROOT),
        "log": "run/arb_scanner.log",
        "heartbeat_stale_sec": 120,  # 30s fixed HB × 4 = safe margin
        # D139: Explicit PYTHONPATH ensures subprocess finds panopticon_py even when
        # watchdog's cwd differs from the project root (e.g. started from different directory)
        "env_pythonpath": str(_PROJECT_ROOT),
    },
}

_POLL_INTERVAL_SEC = 30            # main loop cadence
_DB_MAINT_INTERVAL_SEC = 21600    # 6 hours
_RESTART_WINDOW_SEC = 300          # 5 min circuit breaker window
_MIN_RESTART_GAP_SEC = 10          # floor between restarts
_MAX_RESTARTS_IN_WINDOW = 3


# ── Circuit breaker state ───────────────────────────────────────────────────────

_restart_attempts: dict[str, list[float]] = {}  # name -> list of restart timestamps


# ── Manifest helpers ────────────────────────────────────────────────────────────

def _read_manifest() -> dict:
    try:
        if _MANIFEST.exists():
            return __import__("json").loads(_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ── Process liveness check ──────────────────────────────────────────────────────

def _check_process(name: str, config: dict) -> None:
    """
    Check if a watched process is alive and heartbeat is fresh.
    Restart if either criterion fails.
    """
    manifest = _read_manifest()
    entry = manifest.get(name, {})
    pid = entry.get("pid")

    # Liveness Check 1: PID alive
    pid_alive = pid and is_process_alive(int(pid))

    # Liveness Check 2: Heartbeat fresh
    hb_ts_str = entry.get("last_heartbeat_ts")
    hb_stale = True
    age_sec = 0.0
    if hb_ts_str:
        try:
            hb_dt = datetime.fromisoformat(hb_ts_str)
            if hb_dt.tzinfo is None:
                hb_dt = hb_dt.replace(tzinfo=timezone.utc)
            age_sec = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            hb_stale = age_sec > config["heartbeat_stale_sec"]
        except (ValueError, TypeError):
            hb_stale = True

    if pid_alive and not hb_stale:
        return  # healthy

    reason = []
    if not pid_alive:
        reason.append(f"pid_dead(pid={pid})")
    if hb_stale:
        reason.append(f"hb_stale(age={age_sec:.0f}s > {config['heartbeat_stale_sec']}s)")
    logger.warning("[WATCHDOG] %s unhealthy: %s → restarting", name, ", ".join(reason))
    _restart_process(name, config)


def _restart_process(name: str, config: dict) -> None:
    """Restart a process with circuit breaker protection."""
    now = time.monotonic()
    attempts = _restart_attempts.setdefault(name, [])

    # Circuit breaker: too many restarts in window
    recent = [t for t in attempts if now - t < _RESTART_WINDOW_SEC]
    if len(recent) >= _MAX_RESTARTS_IN_WINDOW:
        logger.error(
            "[WATCHDOG] CIRCUIT_OPEN: %s restarted %d times in %ds — "
            "manual intervention required. Skipping restart.",
            name, len(recent), _RESTART_WINDOW_SEC,
        )
        return

    # Minimum gap check
    if recent and (now - recent[-1]) < _MIN_RESTART_GAP_SEC:
        logger.warning(
            "[WATCHDOG] %s restart suppressed (min gap %ds)",
            name, _MIN_RESTART_GAP_SEC,
        )
        return

    _restart_attempts[name] = recent + [now]

    # D143: Let Windows reclaim handles from the dead PID before spawning a replacement
    # that calls acquire_singleton() against the same PID file (reduces zombie false-positives).
    time.sleep(2.0)

    # D139: Use absolute paths based on _PROJECT_ROOT so log writing is reliable
    # regardless of watchdog's current working directory
    config_cwd = config.get("cwd", str(_PROJECT_ROOT))
    log_path = _PROJECT_ROOT / config["log"]
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # D139: Build env with explicit PYTHONPATH to ensure subprocess finds panopticon_py
    env = {**os.environ, "PYTHONPATH": config.get("env_pythonpath", config_cwd)}

    # D114-1: Use context manager so log_fh is always closed, even on exception.
    # Subprocess inherits a copy of this FD — closing it in parent does not affect child.
    try:
        with open(log_path, "a") as log_fh:
            proc = subprocess.Popen(
                config["cmd"],
                cwd=config_cwd,
                env=env,
                stdout=log_fh,
                stderr=log_fh,
                close_fds=True,
                start_new_session=True,
            )
        # log_fh closed here; child still writes to its inherited copy
        logger.info("[WATCHDOG] %s restarted → PID=%d", name, proc.pid)
    except Exception as exc:
        logger.error("[WATCHDOG] Failed to restart %s: %s", name, exc)


# ── DB maintenance ─────────────────────────────────────────────────────────────

def _run_db_maintenance() -> None:
    """Run SQLite ANALYZE + WAL checkpoint every 6 hours."""
    try:
        from panopticon_py.db import ShadowDB
        db = ShadowDB()
        db.run_maintenance()
        db.close()
        logger.info("[WATCHDOG] DB maintenance complete")
    except Exception as exc:
        logger.warning("[WATCHDOG] DB maintenance failed: %s", exc)


# ── Main loop ────────────────────────────────────────────────────────────────────

def run_watchdog() -> None:
    """Main watchdog loop — poll processes, run maintenance, repeat forever."""
    _RUN_DIR.mkdir(parents=True, exist_ok=True)
    _last_db_maint = 0.0

    logger.info(
        "[WATCHDOG] Starting — watching: %s",
        ", ".join(WATCHED_PROCESSES.keys()),
    )

    while True:
        for name, config in WATCHED_PROCESSES.items():
            _check_process(name, config)

        # Write watchdog's own heartbeat
        try:
            update_heartbeat("watchdog")
        except Exception:
            pass

        # Periodic DB maintenance
        now = time.monotonic()
        if now - _last_db_maint > _DB_MAINT_INTERVAL_SEC:
            _last_db_maint = now
            _run_db_maintenance()

        time.sleep(_POLL_INTERVAL_SEC)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="Panopticon Watchdog Supervisor")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Daemonize (Unix only, ignored on Windows)",
    )
    args = parser.parse_args()

    if args.daemon and sys.platform != "win32":
        # First fork: parent exits, child continues as session leader
        pid = os.fork()
        if pid > 0:
            sys.exit(0)

        # Child becomes session leader (detaches from controlling terminal)
        os.setsid()

        # Second fork: prevent grandchild from re-acquiring a new terminal
        pid = os.fork()
        if pid > 0:
            sys.exit(0)

        # Grandchild: fully daemonized, change to safe directory
        os.chdir("/")
        # Redirect stdio to /dev/null so no unexpected file handle leaks
        devnull = open(os.devnull, "r+")
        os.dup2(devnull.fileno(), sys.stdin.fileno())
        os.dup2(devnull.fileno(), sys.stdout.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())
        devnull.close()

    # D114-3: Register as singleton in manifest so tooling can observe watchdog liveness.
    # In daemon mode: runs in grandchild (after double-fork), so manifest PID is correct.
    from panopticon_py.utils.process_guard import acquire_singleton
    WATCHDOG_VERSION = "v1.0.4-D143"   # ← AGENT: bump on every change  # D138: +arb_scanner to WATCHED_PROCESSES  # D139: parents[2] + PYTHONPATH env  # D143: restart grace sleep before Popen
    acquire_singleton("watchdog", WATCHDOG_VERSION)

    run_watchdog()
