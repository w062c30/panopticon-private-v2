"""
Panopticon HFT Orchestrator — unified single-process launcher (v4-FINAL).

Starts FOUR tracks sharing the same ShadowDB (WAL mode + 30s busy_timeout):

  1. Discovery Loop       (hunting.discovery_loop)
     → finds new wallets, hydrates tier-1 via Moralis funding traces
  2. Polymarket Radar    (hunting.run_radar._live_ticks)
     → captures entropy drops + wallet observations (real taker addresses)
  3. Hyperliquid OFI     (hft.hyperliquid_ws_client)
     → detects UNDERLYING_SHOCK on BTC-USD lead exchange
  4. Graph Linker        (hft.graph_linker)
     → clusters Polymarket takers post-shock into HFT_FIRM_CLUSTER
  5. Signal Engine       (signal_engine._run_async) [asyncio task — NOT subprocess]
     → consensus Bayesian decisions via asyncio.Queue[SignalEvent]

Architecture per [Invariant 1.1/3.1]:
  - L1: asyncio.Queue[SignalEvent] — zero disk I/O, zero-latency event bus
  - L2/L3: signal_engine._run_async — Bayesian consensus, READ-ONLY DB access
  - L4: fast_gate.py — unified single source of truth
  - Observer: analysis_worker — ONLY writer to wallet_market_positions

Wallet linking across tracks:
  Radar     sees:  0xABC → Polymarket CLOB taker (same market)
  Graph     links: 0xABC shares funding root with 0xDEF → HFT_FIRM_CLUSTER
  Discovery resolves: 0xABC → tier1 entity via Moralis

Usage::

  # Normal: Discovery + Radar + OFI + Graph + SE on data/panopticon.db
  python run_hft_orchestrator.py

  # Alongside start_shadow_hydration.py (EXCLUDES signal_engine per Q3 ruling)
  python run_hft_orchestrator.py --db-path data/panopticon_hft.db
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import json
import logging
import os
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from panopticon_py.db import DBWriterQueue, ShadowDB
from panopticon_py.friction_state import FrictionStateWorker, GlobalFrictionState
from panopticon_py.hunting.pol_monitor import PolygonListener
from panopticon_py.hunting.transfer_graph import _write_cu_report
from panopticon_py.hft.graph_linker import HiddenLinkGraphEngine
from panopticon_py.hft.hyperliquid_ws_client import HyperliquidOFIEngine, UnderlyingShock
from panopticon_py.load_env import load_repo_env
from panopticon_py.time_utils import utc_now_rfc3339_ms  # D120: aligns with canonical time utility
from scripts.check_shadow_readiness import check_readiness, ReadinessResult

load_repo_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
# OBS-1: ensure orchestrator INFO logs are always persisted to run/orchestrator.log
_ORCH_LOG_PATH = os.path.join("run", "orchestrator.log")
os.makedirs(os.path.dirname(_ORCH_LOG_PATH), exist_ok=True)
_orch_file_handler = logging.FileHandler(_ORCH_LOG_PATH, encoding="utf-8")
_orch_file_handler.setLevel(logging.INFO)
_orch_file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
)
logging.getLogger().addHandler(_orch_file_handler)
# D78: Singleton enforcement FIRST — kills stale instance before lock-file check
# This must be the first executable line so stale PIDs are cleaned before any exit.
from panopticon_py.utils.process_guard import acquire_singleton, update_heartbeat
PROCESS_VERSION = "v1.7.7-D175"   # D175: inference log retention prune at startup
acquire_singleton("orchestrator", PROCESS_VERSION)

_LOCK_FILE = os.path.join("data", "orchestrator.lock")   # ← orchestrator-specific lock file

# D30: whale scanner enabled by default (can still be explicitly set to 0 by operator env)
os.environ.setdefault("PANOPTICON_WHALE", "1")

logger = logging.getLogger("orchestrator")
_DB_WRITER_HEALTH_PATH = os.getenv("PANOPTICON_WRITER_HEALTH_PATH", "data/async_writer_health.json")
_DB_WRITER_HEARTBEAT_SEC = 5.0
_DB_WRITER_BATCH_MAX = 100
_DB_WRITER_BATCH_TIMEOUT = 0.1
_DB_WRITER_PHASE1_RETRY_DELAY = 0.05
_ISIL_RETENTION_DAYS = int(os.getenv("INSIDER_INFERENCE_RETENTION_DAYS", "30"))

_writer_stats = {
    "batch_count": 0,
    "drop_count": 0,
    "last_batch_size": 0,
    "last_batch_duration_ms": 0,
    "consumer_alive": False,
}
_writer_stats_lock = threading.Lock()


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


# D167 Q1: Radar runs as asyncio task inside this process.
# Write a fresh radar manifest entry at startup so /api/versions reflects live code.
def _init_radar_manifest() -> None:
    """
    D167 Q1 fix: eagerly write radar entry to process_manifest.json at orchestrator
    startup using the live PROCESS_VERSION from run_radar.py.

    This replaces the stale v1.1.41-D119 entry that persisted from 2026-05-01
    because no writer existed to update the radar key.
    """
    from panopticon_py.utils.process_guard import (
        _read_manifest,
        _write_manifest,
        _read_expected_version,
        _version_matches,
    )
    from panopticon_py.hunting import run_radar as _rr

    radar_version = getattr(_rr, "PROCESS_VERSION", "unknown")
    expected = _read_expected_version("radar") or "unknown"
    now = _utc()

    manifest = _read_manifest()
    manifest["radar"] = {
        "pid":               os.getpid(),
        "version":           radar_version,
        "expected":          expected,
        "version_match":     _version_matches(radar_version, expected),
        "host":              socket.gethostname(),
        "status":           "initializing",
        "start_time":        now,
        "last_heartbeat_ts": now,
    }
    _write_manifest("radar", manifest["radar"])
    logger.info(
        "[ORCH][Q1_FIX] radar manifest written: version=%s expected=%s match=%s",
        radar_version,
        expected,
        radar_version == expected,
    )


def _cleanup_lock_file() -> None:
    """Remove lock file only if owned by current PID."""
    try:
        if not os.path.exists(_LOCK_FILE):
            return
        with open(_LOCK_FILE, "r", encoding="utf-8") as f:
            owner_pid = int((f.read() or "").strip())
        if owner_pid == os.getpid():
            os.remove(_LOCK_FILE)
    except Exception:
        pass


def _writer_health_flush(async_writer_health: dict | None = None) -> None:
    """Atomic-write DB writer health snapshot, preserving legacy async writer fields."""
    os.makedirs(os.path.dirname(_DB_WRITER_HEALTH_PATH) or ".", exist_ok=True)
    with _writer_stats_lock:
        snap = dict(_writer_stats)

    payload = {
        "written_at": utc_now_rfc3339_ms(),
        "queue_size": DBWriterQueue.qsize_safe(),
        **snap,
    }
    if async_writer_health:
        payload["running"] = async_writer_health.get("running")
        payload["thread_alive"] = async_writer_health.get("thread_alive")
        payload["queue_depth"] = async_writer_health.get("queue_depth")
        payload["queue_unfinished"] = async_writer_health.get("queue_unfinished")

    tmp = f"{_DB_WRITER_HEALTH_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp, _DB_WRITER_HEALTH_PATH)


def _execute_batch(conn: sqlite3.Connection, items: list) -> int:
    """Execute a batch under one implicit transaction."""
    with conn:
        for item in items:
            conn.execute(item.sql, item.params)
    return len(items)


def _execute_per_item(conn: sqlite3.Connection, items: list) -> int:
    """Phase-2 fallback: execute individually, drop failures."""
    committed = 0
    for item in items:
        try:
            with conn:
                conn.execute(item.sql, item.params)
            committed += 1
        except Exception as exc:
            logger.warning(
                "[DB_WRITER] phase2 drop table=%s sql=%s err=%s",
                item.table_hint,
                item.sql[:80],
                exc,
            )
            with _writer_stats_lock:
                _writer_stats["drop_count"] += 1
    return committed


def _db_writer_thread(db_path: str) -> None:
    """Daemon consumer thread for DBWriterQueue."""
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    q = DBWriterQueue.get()
    last_health = time.monotonic()

    with _writer_stats_lock:
        _writer_stats["consumer_alive"] = True
    logger.info("[DB_WRITER] thread started conn=%s", db_path)

    try:
        while True:
            try:
                first = q.get(timeout=_DB_WRITER_BATCH_TIMEOUT)
            except queue.Empty:
                if time.monotonic() - last_health >= _DB_WRITER_HEARTBEAT_SEC:
                    _writer_health_flush()
                    last_health = time.monotonic()
                continue

            batch = [first]
            sentinel_inside = first is None
            while not sentinel_inside and len(batch) < _DB_WRITER_BATCH_MAX:
                try:
                    nxt = q.get_nowait()
                except queue.Empty:
                    break
                batch.append(nxt)
                if nxt is None:
                    sentinel_inside = True
                    break

            payload_items = [x for x in batch if x is not None]
            t0 = time.monotonic()
            if payload_items:
                try:
                    _execute_batch(conn, payload_items)
                except sqlite3.OperationalError as exc:
                    logger.warning(
                        "[DB_WRITER] batch failed (%s), phase-1 retry after %.0fms",
                        exc,
                        _DB_WRITER_PHASE1_RETRY_DELAY * 1000,
                    )
                    time.sleep(_DB_WRITER_PHASE1_RETRY_DELAY)
                    try:
                        _execute_batch(conn, payload_items)
                    except sqlite3.OperationalError as exc2:
                        logger.warning("[DB_WRITER] phase-1 failed (%s), phase-2 per-item", exc2)
                        _execute_per_item(conn, payload_items)
            duration_ms = int((time.monotonic() - t0) * 1000)

            with _writer_stats_lock:
                _writer_stats["batch_count"] += 1
                _writer_stats["last_batch_size"] = len(payload_items)
                _writer_stats["last_batch_duration_ms"] = duration_ms

            for _ in batch:
                q.task_done()

            if time.monotonic() - last_health >= _DB_WRITER_HEARTBEAT_SEC:
                _writer_health_flush()
                last_health = time.monotonic()

            if sentinel_inside:
                logger.info("[DB_WRITER] sentinel received, exiting")
                break
    except Exception:
        logger.critical("[DB_WRITER] fatal exception, thread dying", exc_info=True)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _writer_stats_lock:
            _writer_stats["consumer_alive"] = False
        _writer_health_flush()
        logger.info("[DB_WRITER] thread exited cleanly")


def _shutdown_writer(thread: threading.Thread) -> None:
    """atexit handler: enqueue sentinel and join writer thread."""
    try:
        DBWriterQueue.enqueue_sentinel()
        thread.join(timeout=10.0)
        if thread.is_alive():
            logger.error("[DB_WRITER] thread did not exit within 10s — items may be lost")
    except Exception as exc:
        logger.warning("[DB_WRITER] atexit shutdown failed: %s", exc)


def _pid_is_alive(pid: int) -> bool:
    """Cross-platform PID existence check without signal side effects."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            return str(pid) in (res.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_lock_file_or_exit() -> None:
    """D30: prevent multi-orchestrator collision in same workspace."""
    os.makedirs(os.path.dirname(_LOCK_FILE), exist_ok=True)
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE, "r", encoding="utf-8") as f:
                old_pid = int((f.read() or "").strip())
            if _pid_is_alive(old_pid):
                print(f"[LOCK] Orchestrator already running as PID {old_pid}. Exiting.")
                sys.exit(1)
            # Stale lock from dead process.
            os.remove(_LOCK_FILE)
        except ValueError:
            os.remove(_LOCK_FILE)

    with open(_LOCK_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(_cleanup_lock_file)


_acquire_lock_file_or_exit()


# ── Global state ──────────────────────────────────────────────────────────────
_close_event = asyncio.Event()
_graph_engine: HiddenLinkGraphEngine | None = None
_procs: list[subprocess.Popen] = []

# ── Mutual-exclusion lock ─────────────────────────────────────────────────────
# Prevents both scripts from running simultaneously (both spawn discovery_loop).
ORCHESTRATOR_LOCK_KEY = "PANOPTICON_ORCHESTRATOR_RUNNING"
HYDRATION_LOCK_KEY = "PANOPTICON_HYDRATION_RUNNING"


def _acquire_advisory_lock(db_path: str, key: str, ttl_sec: int = 3600) -> bool:
    """Acquire an advisory lock row. Returns True if acquired, False if held."""
    import sqlite3
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
        now_str = str(time.time())
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
        row = conn.execute(
            "SELECT pid FROM _process_locks WHERE lock_key = ?", (key,),
        ).fetchone()
        conn.close()
        return row is not None and int(row[0]) == os.getpid()
    except Exception:
        return True  # Fail-open: allow startup if lock check fails


def _release_advisory_lock(db_path: str, key: str) -> None:
    """Release our advisory lock."""
    import sqlite3
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute(
            "DELETE FROM _process_locks WHERE lock_key = ? AND pid = ?",
            (key, os.getpid()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Signal handlers ─────────────────────────────────────────────────────────
def _sigint_handler(sig, frame):
    logger.info("SIGINT received — initiating graceful shutdown")
    _close_event.set()


signal.signal(signal.SIGINT, _sigint_handler)
signal.signal(signal.SIGTERM, _sigint_handler)


# ── Track spawn helpers ──────────────────────────────────────────────────────
def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["LIVE_TRADING"] = "false"
    env["PANOPTICON_DRY_RUN"] = os.getenv("PANOPTICON_DRY_RUN", "1")
    if args is not None and args.db_path:
        env["PANOPTICON_DB_PATH"] = args.db_path
    return env


def _spawn(cmd: list[str], label: str) -> subprocess.Popen:
    p = subprocess.Popen(cmd, env=_env())
    logger.info("[ORCH] Started %s (pid=%d)", label, p.pid)
    return p


args: argparse.Namespace | None = None   # set in main()


# ── Async tracks ─────────────────────────────────────────────────────────────

async def run_polymarket_radar(signal_queue: asyncio.Queue, db: ShadowDB) -> None:
    """Run Polymarket Radar, feeding SignalEvents into signal_queue (zero disk I/O)."""
    from panopticon_py.hunting.run_radar import (
        _live_ticks,
        mark_radar_boot_failure,
    )
    from panopticon_py.hunting.run_radar import _sync_pol_tokens_from_watchlist  # D109: POL immediate startup scan

    # ── D109: POL immediate startup scan (not in _main_async — orchestrator bypasses it) ──
    try:
        pol_tokens = await asyncio.to_thread(_sync_pol_tokens_from_watchlist, db)
        logger.info("[POL][D109] startup scan registered %d t2_pol tokens", len(pol_tokens))
    except Exception as exc:
        logger.warning("[POL][D109] startup scan failed: %s", exc)

    logger.info("[RADAR] Starting Polymarket CLOB WebSocket feed → signal_queue")
    failures: list[float] = []
    restart_window_sec = float(os.getenv("RADAR_BOOT_RESTART_WINDOW_SEC", "300"))
    max_failures = int(os.getenv("RADAR_BOOT_MAX_RESTARTS", "3"))
    cooldown_sec = float(os.getenv("RADAR_BOOT_COOLDOWN_SEC", "120"))
    while True:
        try:
            await _live_ticks(db, signal_queue=signal_queue)
            logger.warning("[RADAR] _live_ticks exited cleanly; restarting in 5s")
            await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            logger.info("[RADAR] Cancelled")
            raise
        except Exception as exc:
            mark_radar_boot_failure(f"boot_failed:{exc!r}")
            logger.error("[RADAR] Fatal error: %s", exc, exc_info=True)
            now = time.monotonic()
            failures = [ts for ts in failures if now - ts <= restart_window_sec]
            failures.append(now)
            if len(failures) >= max_failures:
                logger.error(
                    "[RADAR] Circuit open: %d failures in %.0fs, cooldown %.0fs",
                    len(failures),
                    restart_window_sec,
                    cooldown_sec,
                )
                await asyncio.sleep(cooldown_sec)
                failures.clear()
                continue
            await asyncio.sleep(5.0)


async def run_hyperliquid_ofi(
    signal_queue: asyncio.Queue,
    db: ShadowDB,
    te_cache,
) -> None:
    """Run Hyperliquid OFI engine, mapping shocks to signal_queue (no execution gate)."""
    async def on_shock(shock: UnderlyingShock) -> None:
        from config.ofi_market_map import OFI_MARKET_MAP
        from panopticon_py.signal_engine import SignalEvent

        logger.info(
            "[SHOCK] hl_epoch_ms=%s ofi=%.3f notional=$%.0f price_after=%.4f",
            shock.hl_epoch_ms,
            shock.ofi_value,
            shock.window_total_notional,
            shock.price_after,
        )

        # D81: TE source push — Hyperliquid OFI price_after (O(1), non-blocking)
        if te_cache is not None:
            te_cache.push_source(shock.price_after)

        # Map Hyperliquid market → Polymarket market_ids via static OFI_MARKET_MAP
        pm_market_ids = OFI_MARKET_MAP.get(shock.market_id, [])
        if not pm_market_ids:
            logger.debug("[SHOCK] No OFI_MARKET_MAP entry for %s", shock.market_id)
            return

        # Queue one SignalEvent per mapped Polymarket market
        for pm_market_id in pm_market_ids:
            event = SignalEvent(
                source="ofi",
                market_id=pm_market_id,
                token_id=None,
                ofi_shock_value=shock.ofi_value,
                trigger_address="hyperliquid",
                trigger_ts_utc=datetime.now(timezone.utc).isoformat(),
            )
            await signal_queue.put(event)
            logger.info(
                "[SHOCK→SE] ofi=%.3f market=%s queued",
                shock.ofi_value,
                pm_market_id,
            )

    engine = HyperliquidOFIEngine(on_shock=on_shock)
    logger.info("[OFI] Starting Hyperliquid BTC-USD OFI Engine → signal_queue")
    try:
        await engine.run()
    except asyncio.CancelledError:
        logger.info("[OFI] Cancelled")
    except Exception as exc:
        logger.error("[OFI] Fatal error: %s", exc, exc_info=True)


async def run_graph_linker(db: ShadowDB) -> None:
    global _graph_engine
    _graph_engine = HiddenLinkGraphEngine(db=db)
    logger.info("[GRAPH] HiddenLinkGraphEngine ready")

    while not _close_event.is_set():
        try:
            await asyncio.wait_for(_close_event.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            if _graph_engine is not None:
                try:
                    stats = _graph_engine.engine_stats()
                    logger.debug(
                        "[GRAPH] nodes=%d edges=%d clusters=%d",
                        stats.get("node_count", 0),
                        stats.get("edge_count", 0),
                        stats.get("cluster_count", 0),
                    )
                except Exception:
                    pass


# ── LIVE_TRADING guard ────────────────────────────────────────────────────────

def _check_live_trading_guard(db_path: str) -> ReadinessResult:
    """
    Check readiness before starting in LIVE mode.

    If LIVE_TRADING is set and thresholds are NOT met:
      - Log WARNING
      - Force-fallback LIVE_TRADING to PAPER
      - Return readiness result
    If LIVE_TRADING is not set: return readiness result without forcing anything.
    """
    live_mode = os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes")
    result = check_readiness(db_path)

    if not live_mode:
        return result

    if result.is_ready:
        logger.info(
            "[GUARD] LIVE_TRADING=true — all thresholds met. Proceeding in LIVE mode."
        )
        return result

    # LIVE requested but not ready → force-fallback
    logger.warning(
        "[GUARD] LIVE_TRADING=true but unlock thresholds NOT met: %s",
        result.summary,
    )
    logger.warning(
        "[GUARD] FORCE-FALLBACK: LIVE_TRADING is being cleared."
        " Set LIVE_TRADING again only after thresholds are confirmed via"
        " check_shadow_readiness.py"
    )
    os.environ.pop("LIVE_TRADING", None)
    return result


# ── Main ──────────────────────────────────────────────────────────────────────
async def main_async() -> int:
    global args

    # D167 Q1: Write radar manifest entry before any async tasks start.
    # Must be inside main_async (not module-level) because _utc() is defined in this module.
    _init_radar_manifest()

    logger.info("=" * 60)
    logger.info("Panopticon HFT Orchestrator starting at %s", _utc())
    logger.info("PID: %s  DRY_RUN: %s", os.getpid(), os.getenv("PANOPTICON_DRY_RUN", "1"))
    logger.info("DB: %s", args.db_path if args else "data/panopticon.db")
    logger.info("=" * 60)

    # ── Mutual-exclusion lock ────────────────────────────────────────────────
    db_path = args.db_path if args else "data/panopticon.db"
    if not _acquire_advisory_lock(db_path, ORCHESTRATOR_LOCK_KEY):
        logger.error(
            "[ORCH] Another orchestrator/hydration process is running against the same DB."
            " Stop the other process first. Exit."
        )
        print(
            "\033[31m[ERROR] Another Panopticon process is already running.\033[0m",
            flush=True,
        )
        print(
            "  Stop the other process (Ctrl+C or kill) before starting run_hft_orchestrator.py.",
            flush=True,
        )
        return 1

    # ── LIVE_TRADING unlock guard ─────────────────────────────────────────────
    # Must run before any signal_queue tasks start — ensures readiness or forces fallback.
    readiness = _check_live_trading_guard(db_path)
    logger.info(
        "[GUARD] Shadow readiness: trades=%d win_rate=%s avg_ev=%s",
        readiness.trade_count,
        f"{readiness.win_rate:.1%}" if readiness.win_rate else "N/A",
        f"{readiness.avg_ev_net:+.2f}" if readiness.avg_ev_net else "N/A",
    )

    # ── Shared DB (WAL mode allows concurrent reads) ────────────────────────
    db = ShadowDB(db_path=args.db_path if args else "data/panopticon.db")
    db.bootstrap()
    logger.info("[DB] ShadowDB initialized at %s", db.path)
    try:
        db.prune_insider_score_inference_log(days=_ISIL_RETENTION_DAYS)
    except Exception as exc:
        logger.warning(
            "[ISIL_PRUNE] startup prune failed days=%d err=%s",
            _ISIL_RETENTION_DAYS,
            exc,
        )

    # ── D168: DBWriterQueue consumer thread (daemon + atexit sentinel) ──────
    writer_thread = threading.Thread(
        target=_db_writer_thread,
        args=(str(db.path),),
        daemon=True,
        name="db_writer",
    )
    writer_thread.start()
    atexit.register(_shutdown_writer, writer_thread)

    # ── Transfer Entropy Cache [D81] ─────────────────────────────────────────
    from panopticon_py.signal.transfer_entropy_cache import get_te_cache
    te_cache = get_te_cache()

    def _te_trade_ticks_getter() -> int:
        """Read WS trade tick count from radar module (module-level counter)."""
        try:
            from panopticon_py.hunting import run_radar as _rr
            return getattr(_rr, "_ws_trade_count", 0) or 0
        except Exception:
            return 0

    te_recompute_task = asyncio.create_task(
        te_cache._recompute_loop(trade_ticks_getter=_te_trade_ticks_getter),
        name="te_recompute",
    )
    logger.info("[TE] TransferEntropyCache background task started (interval=15s)")

    # ── Friction state (O(1) read for HFT gate decisions) ──────────────────
    friction_state = GlobalFrictionState()
    friction_worker = FrictionStateWorker(friction_state)
    friction_worker.start()
    logger.info("[FRICTION] GlobalFrictionState worker started")
    await asyncio.sleep(0.3)

    # ── Graph engine — initialized in run_graph_linker() (L317–L318, global).
    #   The local assignment below was dead code (Debt-3); the real engine is
    #   the global _graph_engine set by run_graph_linker(), which runs concurrently.
    # graph_engine = HiddenLinkGraphEngine(db=db)  # Debt-3: removed D126

    # ── AsyncDBWriter — async queue for non-trading DB writes (D119) ───────
    from panopticon_py.db import AsyncDBWriter
    db_writer = AsyncDBWriter(db)
    db_writer.start()
    logger.info("[DB] AsyncDBWriter started")

    # ── Signal Queue — zero-latency event bus [Invariant 1.1] ─────────────
    signal_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    # D170 PATH-B stub — will be populated by wallet engine in D171.
    path_b_alert_queue: asyncio.Queue = asyncio.Queue()
    polygon_outbound: asyncio.Queue = asyncio.Queue(maxsize=5000)
    whale_scanner_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    tg_ingest_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)

    def _persist_writer_health() -> None:
        """D168: Preserve legacy async writer fields while DB writer owns the health file."""
        snap = db_writer.health()
        try:
            _writer_health_flush(async_writer_health=snap)
        except Exception as exc:
            logger.debug("[DB] Could not persist writer health: %s", exc)

    # ── Launch ALL tracks ──────────────────────────────────────────────────
    #   async tasks: Radar, OFI, Graph, Signal Engine (4 tracks in asyncio)
    radar_task    = asyncio.create_task(run_polymarket_radar(signal_queue, db), name="radar")
    ofi_task      = asyncio.create_task(run_hyperliquid_ofi(signal_queue, db, te_cache), name="ofi")
    graph_task    = asyncio.create_task(run_graph_linker(db), name="graph")
    polygon_task  = asyncio.create_task(
        PolygonListener(
            api_key=os.getenv("ALCHEMY_API_KEY", ""),
            outbound=polygon_outbound,
            db_path=str(db.path),
        ).run(),
        name="polygon",
    )

    #   signal_engine as asyncio task (NOT subprocess — per Q11 ruling)
    from panopticon_py import signal_engine as se_module
    se_task = asyncio.create_task(
        se_module._run_async(signal_queue, db),
        name="signal_engine",
    )
    logger.info("[ORCH] Signal engine running as asyncio task (not subprocess)")

    # D169 P2-T2: WhaleScanner + discovery_loop — consume PolygonListener queue
    from panopticon_py.hunting.whale_scanner import WhaleScanner
    from panopticon_py.hunting.discovery_loop import run_discovery_loop as run_discovery_loop_fn
    scanner = WhaleScanner()

    async def _fanout_polygon(
        polygon_outbound_q: asyncio.Queue,
        whale_queue_q: asyncio.Queue,
        tg_queue_q: asyncio.Queue,
    ) -> None:
        while not _close_event.is_set():
            item = await polygon_outbound_q.get()
            try:
                # Use put_nowait + drop-on-full to prevent fanout from blocking
                for q, name in [
                    (whale_queue_q, "whale"),
                    (tg_queue_q, "tg"),
                ]:
                    try:
                        q.put_nowait(item)
                    except asyncio.QueueFull:
                        logger.warning("[FANOUT] %s queue full — dropping event", name)
            finally:
                polygon_outbound_q.task_done()

    fanout_task = asyncio.create_task(
        _fanout_polygon(polygon_outbound, whale_scanner_queue, tg_ingest_queue),
        name="polygon_fanout",
    )
    whale_task = asyncio.create_task(
        scanner.consume_transfers(whale_scanner_queue),
        name="whale_scanner",
    )
    discovery_task = asyncio.create_task(
        run_discovery_loop_fn(),
        name="discovery_loop",
    )
    logger.info("[ORCH] WhaleScanner + discovery_loop launched")

    # NOTE: legacy discovery_loop (scripts/start_shadow_hydration.py) is separate.

    logger.info("[ORCH] All 7 tracks launched — monitoring for shutdown")

    # ── D69 Insider Detection: per-market InsiderDetector track ─────────────
    # Q3 Ruling: Integrate InsiderDetector into orchestrator main loop.
    # Lazily starts detectors when T1 markets become active.
    _insider_detectors: dict = {}  # condition_id -> InsiderDetector

    def _on_insider_alert(alert, db_obj=db):
        """Persist alert to wallet_activity + log.

        D71 Q1: Uses get_canonical_market_id() to resolve COALESCE(market_id, condition_id).
        If canonical ID is None, logs warning and skips UPDATE.
        """
        layer = 1 if "L1" in (alert.trigger or "") else 2 if "L2" in (alert.trigger or "") else 3
        try:
            # D71 Q1: Resolve canonical market ID (handles BTC 5m market_id=NULL case)
            canonical = db_obj.get_canonical_market_id(alert.condition_id)
            if canonical is None:
                logger.warning(
                    "[INSIDER][WARN] canonical_market_id not found for token_id=%s "
                    "cid=%s — skipping wallet_activity UPDATE",
                    (alert.condition_id or "None")[:20],
                    (alert.condition_id or "None")[:20],
                )
                return

            # Debt-1 fix: use ShadowDB's pre-configured connection (WAL mode + busy_timeout=30000)
            # instead of bare sqlite3.connect(timeout=5.0) which bypasses WAL and uses 5s timeout.
            conn = db_obj.conn
            conn.execute(f"""
                UPDATE wallet_activity
                SET insider_l{layer}=1, alert_trigger=?
                WHERE transaction_hash=?
            """, (alert.trigger, alert.tx_hash))
            conn.commit()
        except Exception as e:
            logger.warning("[INSIDER] DB update failed: %s", e)
            try:
                conn.rollback()  # prevent uncommitted transaction from blocking shared connection
            except Exception:
                pass
        logger.warning(
            "[INSIDER ALERT] %s %s $%.0f %s",
            alert.trigger,
            (alert.name or "anon"),
            alert.usd_size,
            (alert.outcome or ""),
        )

    async def run_insider_monitor(db: ShadowDB) -> None:
        """Watch T1 markets and spin up InsiderDetectors for newly active ones.

        D70 Q2: Orphan cleanup — stops detectors for expired condition_ids.
        D70 Q3: Query from polymarket_link_map instead of series_members.
                series_members may not have BTC 5m rows until D71 sync.
                RULE-MKT-3: link_map is now the authoritative T1 market source.
        """
        from panopticon_py.ingestion.insider_detector import InsiderDetector
        import time
        while not _close_event.is_set():
            try:
                # D70 Q3: query link_map directly (no series_members JOIN needed)
                rows = db.execute("""
                    SELECT DISTINCT condition_id
                    FROM polymarket_link_map
                    WHERE market_tier = 't1'
                      AND condition_id IS NOT NULL
                      AND condition_id != ''
                      AND token_id IS NOT NULL
                      AND token_id != ''
                """).fetchall()
                active_cids = {cid for (cid,) in rows}

                # Start new detectors
                for cid in active_cids:
                    if cid not in _insider_detectors:
                        try:
                            det = InsiderDetector(
                                condition_id    = cid,
                                on_alert         = _on_insider_alert,
                                large_trade_usd  = 200.0,
                                rapid_window     = 180,
                                rapid_count      = 3,
                                high_winrate     = 0.70,
                                min_usd          = 10.0,
                            )
                            det.start()
                            _insider_detectors[cid] = det
                            logger.info("[INSIDER] Started for %s", cid[:16])
                        except Exception as e:
                            logger.warning("[INSIDER] Failed to start %s: %s", cid[:16], e)

                # D70 Q2: Stop orphaned detectors (market expired or removed)
                orphans = set(_insider_detectors.keys()) - active_cids
                for cid in orphans:
                    try:
                        _insider_detectors[cid].stop()
                        del _insider_detectors[cid]
                        logger.info("[INSIDER] Stopped orphan detector: %s", cid[:16])
                    except Exception as e:
                        logger.warning("[INSIDER] Failed to stop orphan %s: %s", cid[:16], e)
            except Exception as e:
                logger.warning("[INSIDER] monitor error: %s", e)
            await asyncio.sleep(30.0)

    insider_task = asyncio.create_task(run_insider_monitor(db), name="insider")
    logger.info("[ORCH] InsiderDetector monitor started")

    # ── D171 P4-T2 revised: transfer graph (WSS ingester + one-shot cold-start) ──
    # D171 P4-T2 revised: v1.7.1 — fixes: eth_blockNumber fallback, lookback filter,
    #   fanout put_nowait, noop return, init task monitor
    async def _init_transfer_graph_once(db: ShadowDB):
        from panopticon_py.hunting.entity_linker import EntityLinker
        from panopticon_py.hunting.transfer_graph import (
            TransferGraphIngester,
            init_transfer_graph,
            _write_cu_report,
            COLD_START_LOOKBACK_HOURS,
            SAFE_FALLBACK_BLOCK,
        )
        import aiohttp

        alchemy_key = os.getenv("ALCHEMY_API_KEY", "")
        if not alchemy_key:
            logger.warning("[TRANSFER_GRAPH] ALCHEMY_API_KEY not set — disabled")
            async def _noop():
                pass
            return (
                asyncio.create_task(_noop(), name="transfer_graph_ingester"),
                asyncio.create_task(_noop(), name="transfer_graph_init"),
            )

        linker = EntityLinker()

        def _watchlist_snapshot() -> set[str]:
            rows = db.execute(
                "SELECT wallet_address FROM wallet_watchlist "
                "WHERE wallet_address IS NOT NULL"
            ).fetchall()
            return {str(r[0]).lower() for r in rows if r and r[0]}

        async def _latest_block() -> int:
            """Triple fallback: eth_blockNumber (priority 1) → polygon_sync (2) → safe block (3)."""
            if alchemy_key:
                try:
                    url = f"https://polygon-mainnet.g.alchemy.com/v2/{alchemy_key}"
                    timeout = aiohttp.ClientTimeout(total=5.0)
                    async with aiohttp.ClientSession(timeout=timeout) as s:
                        async with s.post(
                            url,
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "eth_blockNumber",
                                "params": [],
                            },
                        ) as resp:
                            data = await resp.json(content_type=None)
                            if isinstance(data, dict) and data.get("result"):
                                block = int(data["result"], 16)
                                if block > 1_000_000:
                                    logger.info(
                                        "[TG] latest_block from eth_blockNumber: %d",
                                        block,
                                    )
                                    return block
                except Exception as exc:
                    logger.warning(
                        "[TG] eth_blockNumber failed: %s — trying polygon_sync",
                        exc,
                    )
            try:
                row = db.execute(
                    "SELECT last_processed_block FROM polygon_sync "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row[0] and int(row[0]) > 1_000_000:
                    logger.info("[TG] latest_block from polygon_sync: %d", int(row[0]))
                    return int(row[0])
            except Exception as exc:
                logger.warning("[TG] polygon_sync query failed: %s", exc)
            logger.warning(
                "[TG] using SAFE_FALLBACK_BLOCK=%d",
                SAFE_FALLBACK_BLOCK,
            )
            return SAFE_FALLBACK_BLOCK

        def _recently_added_wallets() -> list[str]:
            """
            Returns wallets eligible for cold-start, applying:
              1. Time window filter: first_seen_ts_utc within COLD_START_LOOKBACK_HOURS
              2. Hard cap: at most MAX_COLD_START_WALLETS wallets (most recent first)
                 to ensure Free-tier CU budget is not exceeded.

            Architect Q1 Option B ruling: uses first_seen_ts_utc, not added_ts_utc.

            INVARIANT: This function MUST NOT return more than MAX_COLD_START_WALLETS
                       wallets regardless of time window results.
            """
            import time as _time
            from panopticon_py.hunting.transfer_graph import (
                COLD_START_LOOKBACK_HOURS as _LOOKBACK,
                MAX_COLD_START_WALLETS as _CAP,
            )

            # Hard cap = 0 means cold-start is disabled (WSS-only mode)
            if _CAP == 0:
                logger.info(
                    "[TRANSFER_GRAPH] cold-start disabled "
                    "(TG_COLD_START_MAX_WALLETS=0 — WSS-only mode)"
                )
                return []

            if _CAP < 0:
                logger.warning(
                    "[TRANSFER_GRAPH] cold-start cap is disabled (TG_COLD_START_MAX_WALLETS=%d) "
                    "— Free-tier CU budget may be exceeded",
                    _CAP,
                )

            cutoff_epoch = _time.time() - _LOOKBACK * 3600
            total_count = len(_watchlist_snapshot())

            try:
                # Apply time window first, then LIMIT to cap
                # ORDER BY first_seen_ts_utc DESC: most recently added wallets get priority
                limit_clause = (
                    f"LIMIT {_CAP}"
                    if _CAP > 0
                    else ""
                )
                rows = db.execute(
                    f"""
                    SELECT wallet_address FROM wallet_watchlist
                    WHERE wallet_address IS NOT NULL
                      AND first_seen_ts_utc IS NOT NULL
                      AND first_seen_ts_utc >= datetime(?, 'unixepoch', 'utc')
                    ORDER BY first_seen_ts_utc DESC
                    {limit_clause}
                    """,
                    (cutoff_epoch,),
                ).fetchall()
                wallets = [str(r[0]).lower() for r in rows if r and r[0]]
                logger.info(
                    "[TRANSFER_GRAPH] cold-start eligible: %d wallets "
                    "(lookback=%.0fh, cap=%d, total_watchlist=%d)",
                    len(wallets),
                    _LOOKBACK,
                    _CAP,
                    total_count,
                )
                return sorted(wallets)

            except Exception as exc:
                # Fallback: most recent wallets capped by safe limit
                safe_cap = min(100, _CAP if _CAP > 0 else 50)
                logger.warning(
                    "[TRANSFER_GRAPH] first_seen_ts_utc query failed (%s) — "
                    "falling back to %d most recent wallets",
                    exc,
                    safe_cap,
                )
                try:
                    rows = db.execute(
                        """
                        SELECT wallet_address FROM wallet_watchlist
                        WHERE wallet_address IS NOT NULL
                          AND first_seen_ts_utc IS NOT NULL
                        ORDER BY first_seen_ts_utc DESC
                        LIMIT ?
                        """,
                        (safe_cap,),
                    ).fetchall()
                    return sorted(str(r[0]).lower() for r in rows if r and r[0])
                except Exception as exc2:
                    logger.error(
                        "[TRANSFER_GRAPH] fallback query also failed: %s", exc2
                    )
                    return []

        tg_ingester = TransferGraphIngester(linker=linker, watchlist_fn=_watchlist_snapshot)
        transfer_graph_ingest_task = asyncio.create_task(
            tg_ingester.run(tg_ingest_queue),
            name="transfer_graph_ingester",
        )

        wallets = _recently_added_wallets()
        transfer_graph_init_task = asyncio.create_task(
            init_transfer_graph(
                watchlist=wallets,
                alchemy_key=alchemy_key,
                linker=linker,
                get_latest_block_fn=_latest_block,
            ),
            name="transfer_graph_init",
        )
        logger.info(
            "[TRANSFER_GRAPH] revised tasks launched (ingester + one-shot init), "
            "cold_start_wallets=%d",
            len(wallets),
        )
        return transfer_graph_ingest_task, transfer_graph_init_task

    # ── D171 Q1-A: Fingerprint recompute task ────────────────────────────────
    async def _run_fingerprint_recompute(db: ShadowDB, close_event: asyncio.Event) -> None:
        """
        Run fingerprint_recompute_loop from fingerprint_scrubber.py.
        Passes close_event for graceful D171 orchestrator shutdown.
        """
        from panopticon_py.hunting.fingerprint_scrubber import fingerprint_recompute_loop

        logger.info("[FINGERPRINT] Recompute loop task started")
        try:
            await fingerprint_recompute_loop(close_event=close_event)
        except asyncio.CancelledError:
            logger.info("[FINGERPRINT] Task cancelled")
            raise
        except Exception as exc:
            logger.error("[FINGERPRINT] Fatal error: %s", exc, exc_info=True)
        logger.info("[FINGERPRINT] Task exited")

    # Launch D171 Q1-A tasks
    transfer_graph_ingest_task, transfer_graph_init_task = await _init_transfer_graph_once(db)
    fingerprint_task = asyncio.create_task(
        _run_fingerprint_recompute(db, _close_event),
        name="fingerprint_recompute",
    )
    logger.info("[ORCH] D171: transfer_graph + fingerprint_recompute tasks launched")

    # ── RVF: Pipeline Verification Framework (opt-in only) ──────────────
    # Activated by PANOPTICON_RVF=1 env var. Non-invasive — reads DB + log only.
    if os.getenv("PANOPTICON_RVF") == "1":
        from panopticon_py.verification.rvf_runner import run_rvf_loop
        import glob as _glob

        def _latest_log() -> str:
            matches = _glob.glob("logs/orchestrator_*.log")
            return max(matches, key=os.path.getmtime) if matches else "logs/orchestrator.log"

        rvf_shadow = os.getenv("PANOPTICON_SHADOW") == "1"
        rvf_log_path = _latest_log()
        asyncio.create_task(
            run_rvf_loop(
                db_path=db.path.as_posix(),
                log_path=rvf_log_path,
                interval_sec=300,
                shadow_mode=rvf_shadow,
            ),
            name="rvf",
        )
        logger.info("[RVF] Pipeline Verification Framework started (PANOPTICON_RVF=1) log=%s", rvf_log_path)

    # ── Monitor: restart dead subprocesses; crash on dead async tasks ────────
    async def _restart_signal_engine():
        """Replace crashed signal_engine task with a fresh one."""
        nonlocal se_task
        # Cancel and await the dead task
        se_task.cancel()
        try:
            await asyncio.wait_for(se_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            logger.warning("[ORCH] SE task cleanup: %s", exc)
        # Create new task
        se_task = asyncio.create_task(
            se_module._run_async(signal_queue, db),
            name="signal_engine",
        )
        logger.info("[ORCH] signal_engine restarted")

    # persist writer health every 30s (5s loop × 6)
    _health_persist_counter = 0
    # D171 Q1 follow-up: one-shot cold-start report flag (prevents double-write)
    _tg_init_reported = False

    while True:
        await asyncio.sleep(5.0)
        update_heartbeat("orchestrator")
        update_heartbeat("radar")  # D167 Q1: keep radar entry fresh (same PID as orchestrator)

        _health_persist_counter += 1
        if _health_persist_counter % 6 == 0:
            _persist_writer_health()
            _health_persist_counter = 0

        # No subprocess workers to monitor — discovery_loop runs in start_shadow_hydration.py

        # If any async task crashed, propagate
        crashed = [t for t in [
            radar_task, ofi_task, graph_task, polygon_task, se_task,
            insider_task, te_recompute_task, whale_task, discovery_task,
            fanout_task, transfer_graph_ingest_task, fingerprint_task,
        ] if t.done() and t.exception()]
        for task in crashed:
            logger.error("[ORCH] %s crashed: %s", task.get_name(), task.exception())
            if task is se_task:
                # Restart signal_engine automatically — it crashes on FK constraint
                # which recovers once pending writes settle
                await _restart_signal_engine()
            # For other tasks: log and continue (they are long-running loops)
            # radar/graph are expected to run indefinitely

        # D171 P4-T2 revised: one-shot cold-start completion reporting (only once)
        # _tg_init_reported flag ensures _write_cu_report is called at most once
        if not _tg_init_reported and transfer_graph_init_task.done() and not transfer_graph_init_task.cancelled():
            _tg_init_reported = True  # set BEFORE await to prevent double-write on exception
            exc = transfer_graph_init_task.exception()
            if exc:
                logger.error("[TG] cold-start init task failed: %s", exc)
            else:
                result = transfer_graph_init_task.result()
                if isinstance(result, tuple) and len(result) == 3:
                    attempted, completed, rate_limits = result
                    logger.info(
                        "[TG] cold-start init done: attempted=%d completed=%d rate_limits=%d",
                        attempted, completed, rate_limits,
                    )
                    await _write_cu_report(attempted, completed, rate_limits)
                else:
                    logger.info("[TG] cold-start init done (no stats returned)")

        if _close_event.is_set():
            break

    # ── Graceful shutdown ───────────────────────────────────────────────────
    logger.info("[ORCH] Initiating shutdown")
    _close_event.set()
    DBWriterQueue.enqueue_sentinel()

    for task in [
            radar_task, ofi_task, graph_task, polygon_task, se_task,
            insider_task, te_recompute_task, whale_task, discovery_task,
            fanout_task, transfer_graph_ingest_task, transfer_graph_init_task, fingerprint_task,
        ]:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass

    # D119: Stop AsyncDBWriter after all tasks
    try:
        db_writer.stop()
        logger.info("[DB] AsyncDBWriter stopped")
    except Exception as exc:
        logger.warning("[DB] AsyncDBWriter stop error: %s", exc)

    if writer_thread.is_alive():
        writer_thread.join(timeout=10.0)
        if writer_thread.is_alive():
            logger.error("[DB_WRITER] thread did not exit within 10s during shutdown")

    for p in _procs:
        if p.poll() is None:
            p.terminate()
    time.sleep(0.5)
    for p in _procs:
        if p.poll() is None:
            p.kill()

    friction_worker.stop()
    _release_advisory_lock(db_path, ORCHESTRATOR_LOCK_KEY)
    db.close()
    logger.info("[ORCH] Shutdown complete")
    return 0


def main() -> int:
    global args
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    ap = argparse.ArgumentParser(description="Panopticon HFT Orchestrator")
    ap.add_argument(
        "--db-path",
        default=os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db"),
        help="Path to ShadowDB (default: data/panopticon.db)",
    )
    args = ap.parse_args()

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
