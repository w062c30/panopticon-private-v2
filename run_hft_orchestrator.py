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
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from panopticon_py.db import ShadowDB
from panopticon_py.friction_state import FrictionStateWorker, GlobalFrictionState
from panopticon_py.hft.graph_linker import HiddenLinkGraphEngine
from panopticon_py.hft.hyperliquid_ws_client import HyperliquidOFIEngine, UnderlyingShock
from panopticon_py.load_env import load_repo_env
from scripts.check_shadow_readiness import check_readiness, ReadinessResult

load_repo_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
# D78: Singleton enforcement FIRST — kills stale instance before lock-file check
# This must be the first executable line so stale PIDs are cleaned before any exit.
from panopticon_py.utils.process_guard import acquire_singleton, update_heartbeat
PROCESS_VERSION = "v1.1.19-D84"   # ← AGENT: bump on every change
acquire_singleton("orchestrator", PROCESS_VERSION)

_LOCK_FILE = os.path.join("data", "orchestrator.lock")   # ← orchestrator-specific lock file

# D30: whale scanner enabled by default (can still be explicitly set to 0 by operator env)
os.environ.setdefault("PANOPTICON_WHALE", "1")

logger = logging.getLogger("orchestrator")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    from panopticon_py.hunting.entropy_window import EntropyWindow
    from panopticon_py.hunting.run_radar import _live_ticks

    ew = EntropyWindow()
    logger.info("[RADAR] Starting Polymarket CLOB WebSocket feed → signal_queue")
    try:
        await _live_ticks(ew, db, signal_queue=signal_queue)
    except asyncio.CancelledError:
        logger.info("[RADAR] Cancelled")
    except Exception as exc:
        logger.error("[RADAR] Fatal error: %s", exc, exc_info=True)


async def run_hyperliquid_ofi(
    signal_queue: asyncio.Queue,
    db: ShadowDB,
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

    # ── Friction state (O(1) read for HFT gate decisions) ──────────────────
    friction_state = GlobalFrictionState()
    friction_worker = FrictionStateWorker(friction_state)
    friction_worker.start()
    logger.info("[FRICTION] GlobalFrictionState worker started")
    await asyncio.sleep(0.3)

    # ── Graph engine (shared across async tracks) ──────────────────────────
    graph_engine = HiddenLinkGraphEngine(db=db)

    # ── Signal Queue — zero-latency event bus [Invariant 1.1] ─────────────
    signal_queue: asyncio.Queue = asyncio.Queue()

    # ── Launch ALL tracks ──────────────────────────────────────────────────
    #   async tasks: Radar, OFI, Graph, Signal Engine (4 tracks in asyncio)
    radar_task    = asyncio.create_task(run_polymarket_radar(signal_queue, db), name="radar")
    ofi_task      = asyncio.create_task(run_hyperliquid_ofi(signal_queue, db), name="ofi")
    graph_task    = asyncio.create_task(run_graph_linker(db), name="graph")

    #   signal_engine as asyncio task (NOT subprocess — per Q11 ruling)
    from panopticon_py import signal_engine as se_module
    se_task = asyncio.create_task(
        se_module._run_async(signal_queue, db),
        name="signal_engine",
    )
    logger.info("[ORCH] Signal engine running as asyncio task (not subprocess)")

    # NOTE: discovery_loop is NOT spawned here.
    # It runs as a standalone process via scripts/start_shadow_hydration.py.
    # This orchestrator focuses on real-time pipeline: Radar + OFI + Graph + Signal Engine.
    # Spawning discovery_loop here would cause DB lock if both are run simultaneously.

    logger.info("[ORCH] All 4 tracks launched — monitoring for shutdown")

    # ── D69 Insider Detection: per-market InsiderDetector track ─────────────
    # Q3 Ruling: Integrate InsiderDetector into orchestrator main loop.
    # Lazily starts detectors when T1 markets become active.
    _insider_detectors: dict = {}  # condition_id -> InsiderDetector

    def _on_insider_alert(alert, db_obj=db):
        """Persist alert to wallet_activity + log.

        D71 Q1: Uses get_canonical_market_id() to resolve COALESCE(market_id, condition_id).
        If canonical ID is None, logs warning and skips UPDATE.
        """
        import sqlite3
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

            conn = sqlite3.connect(str(db_obj.path))
            conn.execute(f"""
                UPDATE wallet_activity
                SET insider_l{layer}=1, alert_trigger=?
                WHERE transaction_hash=?
            """, (alert.trigger, alert.tx_hash))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("[INSIDER] DB update failed: %s", e)
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

    while True:
        await asyncio.sleep(5.0)
        update_heartbeat("orchestrator")

        # No subprocess workers to monitor — discovery_loop runs in start_shadow_hydration.py

        # If any async task crashed, propagate
        crashed = [t for t in [radar_task, ofi_task, graph_task, se_task, insider_task]
                  if t.done() and t.exception()]
        for task in crashed:
            logger.error("[ORCH] %s crashed: %s", task.get_name(), task.exception())
            if task is se_task:
                # Restart signal_engine automatically — it crashes on FK constraint
                # which recovers once pending writes settle
                await _restart_signal_engine()
            # For other tasks: log and continue (they are long-running loops)
            # radar/graph are expected to run indefinitely

        if _close_event.is_set():
            break

    # ── Graceful shutdown ───────────────────────────────────────────────────
    logger.info("[ORCH] Initiating shutdown")
    _close_event.set()

    for task in [radar_task, ofi_task, graph_task, se_task, insider_task]:
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            pass

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
