"""
Spawns shadow-mode Observer subprocesses: discovery_loop and analysis_worker.

Pre-warms wallet_observations / insider_score_snapshots before
run_hft_orchestrator.py starts (L2/L3 data pipeline warmup).

IMPORTANT: This script and run_hft_orchestrator.py both attempt to spawn
discovery_loop against the same DB. Run ONE at a time:
  1. start_shadow_hydration.py  → warmup (then stop with Ctrl+C)
  2. run_hft_orchestrator.py   → full system (Radar + OFI + Graph + SE)

Do NOT run both simultaneously — a DB-advisory-lock prevents hard crashes,
but the second process will exit with a clear error.

定位 [Q3 Ruling]:
  降级为纯 Observer 启 动器（资料管道预热工具），
  绝对不允许启动任何 signal_engine 实例或 L4 执行逻辑。

启动的进程：
  - discovery_loop.py (T1): 发现新钱包、补充 tier-1
  - analysis_worker.py (T5): LIFO 仓位追踪 + insider scoring

signal_engine 已整合进 run_hft_orchestrator.py 作为 asyncio task。

Usage::

  # Terminal 1: Warm up data (Ctrl+C to stop when ready)
  python scripts/start_shadow_hydration.py

  # Terminal 2: Full system (run while terminal 1 is stopped)
  python run_hft_orchestrator.py
"""

import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time


SYSTEM_STATUS_LINE = "[SYSTEM_STATUS] Shadow Mode Active (Observer Only). Hydrating Seed_Whitelist..."
ORCHESTRATOR_LOCK_KEY = "PANOPTICON_ORCHESTRATOR_RUNNING"
HYDRATION_LOCK_KEY = "PANOPTICON_HYDRATION_RUNNING"
LOCK_TTL_SEC = 3600  # 1 hour — safe upper bound for any single run
logger = logging.getLogger(__name__)


def _acquire_advisory_lock(db_path: str, key: str, ttl_sec: int = LOCK_TTL_SEC) -> bool:
    """
    Attempt to acquire an advisory lock row in the DB.
    Returns True if acquired, False if another process holds the lock.
    Uses an INSERT-with-ONCONFLICT to atomically claim or detect.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        now_str = str(time.time())
        try:
            conn.execute(
                f"""
                INSERT INTO _process_locks (lock_key, pid, acquired_at, ttl_sec)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                  pid=excluded.pid,
                  acquired_at=excluded.acquired_at,
                  ttl_sec=excluded.ttl_sec
                """,
                (key, os.getpid(), now_str, ttl_sec),
            )
            conn.commit()
            # Verify no stale lock (in case the other process died without cleaning)
            row = conn.execute(
                "SELECT pid, acquired_at FROM _process_locks WHERE lock_key = ?",
                (key,),
            ).fetchone()
            if row and int(row[0]) == os.getpid():
                conn.close()
                return True
            conn.close()
            return False
        except sqlite3.OperationalError:
            conn.close()
            return False
    except Exception:
        return False  # If we can't check, allow startup (fail-open)


def _release_advisory_lock(db_path: str, key: str) -> None:
    """Release our advisory lock on exit."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute("DELETE FROM _process_locks WHERE lock_key = ? AND pid = ?", (key, os.getpid()))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _ensure_lock_table(db_path: str) -> None:
    """Create the advisory lock table if it doesn't exist."""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _process_locks (
                lock_key TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                ttl_sec INTEGER NOT NULL DEFAULT 3600
            )
            """
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def ensure_shadow_mode_env() -> dict[str, str]:
    env = dict(os.environ)
    live = env.get("LIVE_TRADING", "false").strip().lower()
    if live in {"1", "true", "yes"}:
        raise RuntimeError("LIVE_TRADING must be false before starting shadow hydration")
    env["LIVE_TRADING"] = "false"
    if not env.get("DISCOVERY_PROVIDER", "").strip():
        env["DISCOVERY_PROVIDER"] = "dual_track"
    if not env.get("DISCOVERY_COLD_START_INTERVAL_HOURS", "").strip():
        env["DISCOVERY_COLD_START_INTERVAL_HOURS"] = "2"
    if not env.get("DISCOVERY_COLD_START_WINDOW_HOURS", "").strip():
        env["DISCOVERY_COLD_START_WINDOW_HOURS"] = "48"
    if not env.get("DISCOVERY_RELAXED_INTERVAL_HOURS", "").strip():
        env["DISCOVERY_RELAXED_INTERVAL_HOURS"] = "6"
    if not env.get("DISCOVERY_TIER1_THRESHOLD", "").strip():
        env["DISCOVERY_TIER1_THRESHOLD"] = "100"
    if not env.get("DISCOVERY_HISTORY_MIN_OBS", "").strip():
        env["DISCOVERY_HISTORY_MIN_OBS"] = "20"
    if not env.get("DISCOVERY_HTTP_TIMEOUT_SEC", "").strip():
        env["DISCOVERY_HTTP_TIMEOUT_SEC"] = "15"
    if not env.get("HUNT_ENTROPY_GAP_FLUSH_SEC", "").strip():
        env["HUNT_ENTROPY_GAP_FLUSH_SEC"] = "30.0"
    if not env.get("HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC", "").strip():
        env["HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC"] = "15.0"
    return env


def _spawn(cmd: list[str], env: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(cmd, env=env)


def main() -> int:
    db_path = os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db")
    _ensure_lock_table(db_path)

    if not _acquire_advisory_lock(db_path, HYDRATION_LOCK_KEY):
        print(
            "\033[31m[ERROR] Another Panopticon hydration process is already running.\033[0m",
            flush=True,
        )
        print(
            "  Stop the other process (Ctrl+C or kill) before starting a new one.",
            flush=True,
        )
        print(
            "  If the previous process crashed, the stale lock will expire in 1 hour.",
            flush=True,
        )
        return 1

    try:
        env = ensure_shadow_mode_env()
        provider = env.get("DISCOVERY_PROVIDER", "mock")
        print(SYSTEM_STATUS_LINE, flush=True)
        print(f"[SYSTEM_STATUS] DISCOVERY_PROVIDER is set to: {provider}", flush=True)
        if provider.strip().lower() == "mock":
            print("\033[33m[WARNING] DISCOVERY_PROVIDER=mock (not real data provider)\033[0m", flush=True)

        py = sys.executable

        # T1: Discovery Loop (finds smart money wallets) — per Q3 ruling
        effective_provider = "dual_track"
        env["DISCOVERY_PROVIDER"] = effective_provider
        discovery_cmd = [py, "-m", "panopticon_py.hunting.discovery_loop", "--provider", effective_provider]
        # T5: Analysis Worker (LIFO position tracking + insider scoring)
        analysis_cmd = [py, "-m", "panopticon_py.ingestion.analysis_worker"]

        procs: list[subprocess.Popen] = []
        cmd_labels: list[str] = []

        procs.append(_spawn(discovery_cmd, env))
        cmd_labels.append("discovery_loop")
        procs.append(_spawn(analysis_cmd, env))
        cmd_labels.append("analysis_worker")

        # signal_engine is NOT spawned here — it runs as asyncio task in run_hft_orchestrator.py

        def _shutdown(*_args: object) -> None:
            _release_advisory_lock(db_path, HYDRATION_LOCK_KEY)
            for p in procs:
                if p.poll() is None:
                    p.terminate()
            time.sleep(0.3)
            for p in procs:
                if p.poll() is None:
                    p.kill()
            raise SystemExit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        print(f"[SYSTEM_STATUS] Observer processes: {cmd_labels}", flush=True)
        print(
            "\033[33m[INFO] Stop this (Ctrl+C) before starting run_hft_orchestrator.py to avoid DB lock.\033[0m",
            flush=True,
        )

        while True:
            time.sleep(1.0)
            dead_indices: list[int] = []
            for i, p in enumerate(procs):
                rc = p.poll()
                if rc is not None and rc != 0:
                    logger.warning(
                        "[SYSTEM_STATUS] %s (index=%d) died with exit code %d, restarting...",
                        cmd_labels[i], i, rc,
                    )
                    dead_indices.append(i)
            if dead_indices:
                cmd_map = {0: discovery_cmd, 1: analysis_cmd}
                for i in dead_indices:
                    procs[i] = _spawn(cmd_map[i], env)
                time.sleep(1.0)
            else:
                continue
    except KeyboardInterrupt:
        pass
    finally:
        _release_advisory_lock(db_path, HYDRATION_LOCK_KEY)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
