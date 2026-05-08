"""
Panopticon Signal Engine — zero-latency event-driven consensus Bayesian decision actor (v5.2.0-D171).

D171: w4 fund_source_score (NQ-1 weighted blend in _get_insider_score) +
      PATH-B alert with 24h hysteresis (submit_path_b_alert).

D170: Added L4 signal fusion (Alert + L4Fuser) — PATH-A live, PATH-B stub.
      Fixed dataclasses.replace import (B4 NameError in shadow mode).

Event sources (via asyncio.Queue — ZERO disk I/O) [Invariant 1.1]:
  - Polymarket Radar: entropy drop event (source="radar")
  - Hyperliquid OFI: underlying shock event (source="ofi") — OFI fast path

CRITICAL INVARIANTS:
  - wallet_market_positions is READ-ONLY here (updated only by analysis_worker) [Invariant 3.1]
  - execution_records is WRITE-ONLY (records our system trades)
  - paper_trade / LIFO updates are FORBIDDEN in signal_engine [Invariant 5.1]
  - OFI path must still run Bayesian consensus — no fixed p=0.95 [Invariant 3.1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import threading

logger = logging.getLogger(__name__)
import os
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

# D160-2: async-safe SQLite lock retry for use inside async functions
_DB_LOCK_MAX_RETRIES = 3
_DB_LOCK_RETRY_SLEEP_S = 0.05


async def _db_execute_async_retry(
    conn, sql: str, params=(), max_retries: int = _DB_LOCK_MAX_RETRIES,
    sleep_s: float = _DB_LOCK_RETRY_SLEEP_S,
):
    """
    D160-2: Async-safe bounded retry for SQLite OperationalError ``database is locked``.
    Uses ``await asyncio.sleep()`` — safe inside async functions (does NOT block event loop).
    Re-raises immediately for any non-lock OperationalError.
    """
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= max_retries - 1:
                raise
            await asyncio.sleep(sleep_s * (attempt + 1))

from panopticon_py.db import ShadowDB
from panopticon_py.execution.clob_client import submit_fok_order
from panopticon_py.execution.constants import (
    REASON_INSIDER_BYPASS_PAPER,
    REASON_INSUFFICIENT_CONSENSUS,
    REASON_KELLY_DEGRADED_PREFIX,
    REASON_NO_PRICE_DATA,
    REASON_T1_SHORT_CIRCUIT,
    REASON_Z_MAGNITUDE_BELOW_THRESHOLD,
)
from panopticon_py.fast_gate import FastSignalInput, GateDecision, fast_execution_gate
from panopticon_py.friction_state import FrictionSnapshot
from panopticon_py.ingestion.clob_client import fetch_best_ask
from panopticon_py.time_utils import utc_now_rfc3339_ms

# ---------------------------------------------------------------------------
# Price cache — Polymarket CLOB /book endpoint with spread-based selection
# ---------------------------------------------------------------------------
_PRICE_CACHE: dict[str, tuple[float | None, float]] = {}
_PRICE_CACHE_TTL = 30.0
_CLOB_BOOK_URL = "https://clob.polymarket.com/book"
_SPREAD_THRESHOLD = 0.10  # mirrors Polymarket UI switch point


def _mc():
    """Lazy MetricsCollector getter — avoids circular import."""
    try:
        from panopticon_py.metrics import get_collector
        return get_collector()
    except Exception:
        return None


def _te_cache():
    """Lazy TransferEntropyCache getter — returns the singleton instance. [Invariant 4.2]"""
    try:
        from panopticon_py.signal.transfer_entropy_cache import get_te_cache
        return get_te_cache()
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MIN_CONSENSUS_SOURCES = int(os.getenv("MIN_CONSENSUS_SOURCES", "2"))
INSIDER_SCORE_THRESHOLD = float(os.getenv("INSIDER_SCORE_THRESHOLD", "0.55"))
ENTROPY_LOOKBACK_SEC = int(os.getenv("ENTROPY_LOOKBACK_SEC", "1800"))  # D96: was 360, increased to cover data-api polling cadence
MIN_ENTROPY_Z_THRESHOLD = float(os.getenv("MIN_ENTROPY_Z_THRESHOLD", "-4.0"))
DEFAULT_CAPITAL = 100.0
KELLY_FRACTION = 0.25
D167_TAG = "D167-dry-run-signal"
D170_TAG = "D170-l4-fusion"
PROCESS_VERSION = "v5.2.0-D171"  # D171 Q2-A: w4 fund_source_score + Q3-A: PATH-B hysteresis
L4_WINDOW_SEC = float(os.getenv("PANOPTICON_L4_WINDOW_SEC", "300"))
L4_BOOST_FACTOR = float(os.getenv("PANOPTICON_L4_BOOST_FACTOR", "1.5"))
L4_BOOST_CAP = float(os.getenv("PANOPTICON_L4_BOOST_CAP", "1.0"))

# D167: one-shot dry-run trigger + z-distribution observability ring
_DRY_RUN_SIGNAL = os.getenv("PANOPTICON_DRY_RUN_SIGNAL", "0").lower() in ("1", "true", "yes")
_DRY_RUN_FIRED = False
_DRY_RUN_SYNTHETIC_EMITTED = False
_SIGNAL_FIRED_COUNT = 0
_Z_OBSERVED_RING: list[tuple[str, float]] = []
_Z_RING_MAX = 1000
_Z_FLUSH_EVERY = 20
_Z_LOCK = threading.Lock()


def _flush_z_distribution(path: str = "data/z_distribution.json") -> None:
    """Atomic write of recent z observations for D167 P0-T3 diagnostics."""
    with _Z_LOCK:
        payload = {
            "updated_ts": utc_now_rfc3339_ms(),
            "count": len(_Z_OBSERVED_RING),
            "samples": list(_Z_OBSERVED_RING),
        }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def _record_z_and_maybe_flush(asset_short: str, z: float) -> None:
    """Append z to ring and periodically flush to JSON."""
    if z is None:
        return
    should_flush = False
    with _Z_LOCK:
        _Z_OBSERVED_RING.append((asset_short, float(z)))
        if len(_Z_OBSERVED_RING) > _Z_RING_MAX:
            del _Z_OBSERVED_RING[: len(_Z_OBSERVED_RING) - _Z_RING_MAX]
        should_flush = (len(_Z_OBSERVED_RING) % _Z_FLUSH_EVERY) == 0
    if should_flush:
        _flush_z_distribution()


def _maybe_dry_run_override(z: float) -> tuple[float, bool]:
    """Return (effective_z, is_forced) for one-shot dry-run firing."""
    global _DRY_RUN_FIRED
    if _DRY_RUN_SIGNAL and not _DRY_RUN_FIRED:
        _DRY_RUN_FIRED = True
        forced = MIN_ENTROPY_Z_THRESHOLD - 1.0
        logger.warning(
            "[DRY_RUN_FIRE] forcing effective_z=%.3f (real_z=%.3f) one-shot consumed",
            forced,
            z,
        )
        return forced, True
    return z, False

# D108: Schema-sync constant — must match execution_records CHECK constraint
_VALID_EXECUTION_SOURCES: frozenset[str] = frozenset({"radar", "ofi"})


def _live_trading_env_on() -> bool:
    return os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes")


def _insider_bypass_tiers_effective() -> frozenset[str]:
    """D159-3: INSIDER_BYPASS_TIERS e.g. ``t2,t3``. Empty = disabled. Never active when LIVE_TRADING."""
    if _live_trading_env_on():
        return frozenset()
    raw = os.getenv("INSIDER_BYPASS_TIERS", "").strip().lower()
    if not raw:
        return frozenset()
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


# D160-3: Per-mode insider score threshold
# LIVE: uses INSIDER_SCORE_THRESHOLD (0.55, production rigor).
# PAPER: uses PAPER_INSIDER_SCORE_THRESHOLD (default 0.30) to allow more signals through.
# Enforced floor: PAPER threshold must be >= 0.15 to avoid noise flooding.
_PAPER_RAW = float(os.getenv("PAPER_INSIDER_SCORE_THRESHOLD", "0.30"))
if _PAPER_RAW < 0.15:
    raise ValueError(
        f"PAPER_INSIDER_SCORE_THRESHOLD must be >= 0.15, got {_PAPER_RAW}"
    )


def _effective_insider_threshold() -> float:
    """
    D160-3: Returns the active INSIDER_SCORE_THRESHOLD for this run mode.
    - LIVE_TRADING=true  → INSIDER_SCORE_THRESHOLD (default 0.55)
    - PAPER / shadow     → PAPER_INSIDER_SCORE_THRESHOLD (default 0.30, floor 0.15)
    """
    if _live_trading_env_on():
        return float(os.getenv("INSIDER_SCORE_THRESHOLD", "0.55"))
    return _PAPER_RAW

# ---------------------------------------------------------------------------
# Shadow Mode Parameters (Phase 2 data collection acceleration)
# ---------------------------------------------------------------------------
# Applied when LIVE_TRADING != true to allow more signals through during
# shadow mode data collection. Kyle's λ calibrated to real Polymarket
# magnitude (0.00001 vs system high-estimate of 0.001 = 250x overestimate).
# REVERT TO PRODUCTION VALUES when LIVE_TRADING=true.
_SHADOW_KYLE_LAMBDA = 0.00001
_SHADOW_SLIPPAGE_TOLERANCE = 0.05
_SHADOW_ORDER_SIZE_USD = 10.0  # lowered from 25.0 to reduce theoretical slippage


def _t5_time_decay_weight(end_time_ts: float) -> float:
    """
    D118: Time-to-event decay weight for T5 scoring.
    Must match panopticon_py.hunting.run_radar._t5_time_decay_weight.
    """
    tte_hours = max(0.0, (end_time_ts - time.time()) / 3600.0)
    if tte_hours < 6:
        return 1.0
    elif tte_hours < 24:
        return 0.85
    elif tte_hours < 72:
        return 0.65
    elif tte_hours < 168:
        return 0.45
    else:
        return 0.30

# ---------------------------------------------------------------------------
# Diagnostic: Print static EV config once at startup
# ---------------------------------------------------------------------------


def _diag_print_ev_config() -> None:
    """
    Prints all EV-related static configuration parameters once at startup.
    Used for diagnosing ev_net = -4975 magnitude errors.
    """
    import json

    live_trading = os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes")

    config = {
        "LIVE_TRADING": live_trading,
        "shadow_mode": not live_trading,
        "DEFAULT_CAPITAL": DEFAULT_CAPITAL,
        "KELLY_FRACTION": KELLY_FRACTION,
        "MIN_ENTROPY_Z_THRESHOLD": MIN_ENTROPY_Z_THRESHOLD,
        "MIN_CONSENSUS_SOURCES": MIN_CONSENSUS_SOURCES,
        "INSIDER_SCORE_THRESHOLD": INSIDER_SCORE_THRESHOLD,
        "INSIDER_SCORE_EFFECTIVE": _effective_insider_threshold(),  # paper vs live mode
        "PAPER_INSIDER_SCORE_THRESHOLD": _PAPER_RAW,
        # Effective signal params (shadow vs production)
        "kyle_lambda": _SHADOW_KYLE_LAMBDA if not live_trading else "PRODUCTION_CALIBRATED",
        "slippage_tolerance": _SHADOW_SLIPPAGE_TOLERANCE if not live_trading else 0.009,
        "order_size_usd": _SHADOW_ORDER_SIZE_USD if not live_trading else DEFAULT_CAPITAL * KELLY_FRACTION,
        "default_signal_params": {
            "payout": 1.0,
            "delta_t_ms": 150.0,
            "gamma": 0.001,
            "slippage_tolerance": _SHADOW_SLIPPAGE_TOLERANCE if not live_trading else 0.009,
            "min_ev_threshold": 0.0,
            "daily_opp_cost": 0.0008,
            "days_to_resolution": 3.0,
        },
    }
    logger.info("[DIAG][EV_CONFIG] %s", json.dumps(config, indent=2))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SignalEvent:
    """
    Zero-copy event fed into signal_engine via asyncio.Queue.
    Produced by: Radar (_live_ticks), OFI engine (on_shock callback)
    Consumed by: signal_engine (_run_async)
    """
    source: str             # "radar" | "ofi" | "db_poll_fallback"
    market_id: str          # Polymarket market_id
    token_id: str | None    # CLOB token_id
    entropy_z: float | None = None   # z-score from Polymarket entropy drop (radar)
    ofi_shock_value: float | None = None  # OFI shock value (ofi engine)
    trigger_address: str = "system"
    trigger_ts_utc: str | None = None
    market_tier: str = "t3"   # "t1"|"t2"|"t3"|"t5" — used for p_prior override
    series_id: str = ""      # e.g. "btc-updown-5m", "iran-peace-deal" — D21 metadata
    window_ts: int = 0      # Unix timestamp of T1 window start (0 if not T1)
    time_to_event: float = 0.0  # seconds until market settlement (T5 weighting)

    @property
    def z(self) -> float:
        """Canonical score — use entropy_z or |ofi_shock_value|."""
        if self.entropy_z is not None:
            return abs(self.entropy_z)
        if self.ofi_shock_value is not None:
            return abs(self.ofi_shock_value)
        return 0.0


@dataclass
class Alert:
    source: str            # PATH_A | PATH_B
    market_id: str
    direction: str         # YES | NO
    confidence: float      # 0..1 pre-normalized by caller
    raw_z: Optional[float] = None
    raw_wallet: Optional[str] = None
    received_at: float = field(default_factory=time.monotonic)
    voided: bool = False

    def __post_init__(self) -> None:
        if self.source not in ("PATH_A", "PATH_B"):
            raise ValueError(f"[D170] Alert.source must be PATH_A or PATH_B, got {self.source!r}")
        if self.direction not in ("YES", "NO"):
            raise ValueError(f"[D170] Alert.direction must be YES or NO, got {self.direction!r}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"[D170] Alert.confidence must be 0..1, got {self.confidence!r}")


class L4Fuser:
    def __init__(self) -> None:
        self._window: dict[str, List[Alert]] = {}
        self._lock = threading.Lock()

    def _purge_expired(self, market_id: str, now_mono: float) -> None:
        kept = [
            a for a in self._window.get(market_id, [])
            if max(0.0, now_mono - a.received_at) <= L4_WINDOW_SEC and not a.voided
        ]
        if kept:
            self._window[market_id] = kept
        else:
            self._window.pop(market_id, None)

    def submit(self, alert: Alert) -> Optional[Alert]:
        if not (0.0 < alert.confidence <= 1.0):
            raise ValueError(f"confidence out of range: {alert.confidence}")

        with self._lock:
            now_mono = time.monotonic()
            self._purge_expired(alert.market_id, now_mono)
            recent = self._window.get(alert.market_id, [])

            if not recent:
                self._window.setdefault(alert.market_id, []).append(alert)
                return alert

            most_recent = recent[-1]
            age = now_mono - most_recent.received_at

            if most_recent.source == alert.source:
                logger.info(
                    "[L4_DEDUP] market=%s source=%s age_sec=%.1f dropping",
                    alert.market_id, alert.source, age,
                )
                return None

            if most_recent.direction == alert.direction:
                boosted = min(alert.confidence * L4_BOOST_FACTOR, L4_BOOST_CAP)
                fused = Alert(
                    source=alert.source,
                    market_id=alert.market_id,
                    direction=alert.direction,
                    confidence=boosted,
                    raw_z=alert.raw_z,
                    raw_wallet=alert.raw_wallet,
                    received_at=now_mono,
                )
                logger.info(
                    "[L4_BOOST] market=%s source=%s direction=%s confidence=%.2f (was %.2f) prior=%s age_sec=%.1f",
                    alert.market_id, alert.source, alert.direction,
                    boosted, alert.confidence, most_recent.source, age,
                )
                self._window[alert.market_id].append(fused)
                return fused

            most_recent.voided = True
            logger.warning(
                "[L4_SKIP_OPPOSITE] market=%s path_a=%s path_b=%s age_sec=%.1f",
                alert.market_id, most_recent.direction, alert.direction, age,
            )
            return None


_l4_fuser = L4Fuser()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _consensus_bayesian_update(
    sources: list[float],
    prior: float = 0.5,
    extra_sources: int = 0,
) -> tuple[float, float]:
    """
    Geometric-mean likelihood ratio from independent insider-score sources,
    followed by a single Bayesian posterior update.

    [D81] extra_sources: number of additional independent observations
    (e.g. TE significant flag encoded as n=1) added to the geometric mean denominator.
    [Invariant 4.2] TE is encoded as bool→int (O(1) read from te_cache.is_significant).
    """
    n = len(sources) + extra_sources
    if n == 0:
        return prior, 0.0

    log_lr_sum = sum(math.log(s) - math.log(1.0 - s) for s in sources)
    lr = math.exp(log_lr_sum / n) if n > 0 else 1.0
    lr = max(0.1, min(20.0, lr))

    posterior = prior * lr / (prior * lr + (1.0 - prior))
    posterior = max(0.001, min(0.999, posterior))
    return posterior, lr



def _get_current_price(market_id: str, token_id: str | None, db: ShadowDB) -> float | None:
    """
    Query Polymarket CLOB /book endpoint for real-time price.
    Falls back to resolving token_id from polymarket_link_map if not provided.
    Price selection mirrors Polymarket UI:
      - spread <= 0.10 → mid_price  (tight market)
      - spread >  0.10 → last_trade_price (if real trades exist)
      - no data        → None
    Cache TTL: 30 seconds.
    """
    resolved_token_id: str | None = token_id
    if resolved_token_id is None and db is not None:
        link = db.get_link_mapping_by_market_id(market_id)
        if link and link.get("token_id"):
            resolved_token_id = link["token_id"]

    if resolved_token_id is None:
        logging.debug("[SE] No token_id for market_id=%s", market_id)
        _notify_price_fetch(source="no_price", spread=None)
        return None

    now = time.monotonic()
    cached = _PRICE_CACHE.get(resolved_token_id)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL:
        return cached[0]

    price, spread = _fetch_clob_price_with_spread(resolved_token_id)
    _PRICE_CACHE[resolved_token_id] = (price, now)

    if price is not None:
        source = "mid" if spread is not None and spread <= _SPREAD_THRESHOLD else "last_trade"
        _notify_price_fetch(source=source, spread=spread)
    else:
        _notify_price_fetch(source="no_price", spread=spread)

    return price


def _fetch_clob_price_with_spread(token_id: str) -> tuple[float | None, float | None]:
    """
    Thin wrapper around _fetch_clob_price that also returns the spread.
    Used by signal_engine to feed metrics_collector.
    """
    import httpx
    for attempt in range(3):
        try:
            resp = httpx.get(
                _CLOB_BOOK_URL,
                params={"token_id": token_id},
                timeout=2.0,
            )
            if resp.status_code != 200:
                break
            book = resp.json()
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            last = book.get("last_trade_price")

            if not bids or not asks:
                return None, None

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            spread = best_ask - best_bid

            if spread <= _SPREAD_THRESHOLD:
                return (best_bid + best_ask) / 2.0, spread

            if last and last != "0.5":
                return float(last), spread
            return None, spread

        except Exception:
            if attempt < 2:
                import time as _t
                _t.sleep(0.5)
    return None, None


def _notify_price_fetch(source: str, spread: float | None) -> None:
    """Send price fetch result to MetricsCollector for dashboard display."""
    mc = _mc()
    if mc:
        mc.on_price_fetch_result(source, spread)


async def _get_insider_score(wallet: str, db: ShadowDB) -> float | None:
    """
    D171 Q2-A: Composite insider score with 5 weighted components (NQ-1 ruling).
    w1=0.30 (velocity) + w2=0.25 (consistency) + w3=0.20 (fingerprint size_entropy)
        + w4=0.15 (fund_source_graph) + w5=0.10 (fingerprint timing_entropy)
    = 1.00

    w4 reads from transfer_graph table (D171 P4-T2); falls back to 0.0 if graph
    not yet warmed for this address.
    Falls back to legacy single-score lookups when score_components_json is empty.
    """
    wallet_lc = wallet.lower()

    # ── w1 + w2: velocity + consistency from insider_score_snapshots ─────
    w1, w2 = 0.0, 0.0
    try:
        row = (await _db_execute_async_retry(
            db.conn,
            """
            SELECT score FROM insider_score_snapshots
            WHERE address = ?
            ORDER BY ingest_ts_utc DESC
            LIMIT 1
            """,
            (wallet_lc,),
        )).fetchone()
        if row is not None:
            w1 = float(row[0] or 0.0)
    except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError) as exc:
        logger.debug("[SE][INSIDER] w1 read error addr=%s: %s", wallet_lc[:10], exc)

    try:
        row2 = (await _db_execute_async_retry(
            db.conn,
            """
            SELECT score FROM insider_score_snapshots
            WHERE address = ? AND score != ?
            ORDER BY ingest_ts_utc DESC
            LIMIT 1
            """,
            (wallet_lc, w1),
        )).fetchone()
        if row2 is not None and row2[0] is not None:
            w2 = float(row2[0])
        else:
            w2 = w1  # fallback: single score → split evenly
    except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError) as exc:
        logger.debug("[SE][INSIDER] w2 read error addr=%s: %s", wallet_lc[:10], exc)

    # ── w3 + w5: fingerprint entropy from wallet_watchlist.score_components_json ─
    w3, w5 = 0.0, 0.0
    try:
        comp_row = (await _db_execute_async_retry(
            db.conn,
            "SELECT score_components_json FROM wallet_watchlist WHERE wallet_address = ?",
            (wallet_lc,),
        )).fetchone()
        if comp_row and comp_row[0]:
            comps = json.loads(comp_row[0])
            w3 = max(0.0, min(1.0, float(comps.get("size_entropy", 0.0))))
            w5 = max(0.0, min(1.0, float(comps.get("timing_entropy", 0.0))))
    except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as exc:
        logger.debug("[SE][INSIDER] w3/w5 read error addr=%s: %s", wallet_lc[:10], exc)

    # ── w4: fund_source_score from transfer_graph (D171 P4-T2) ───────────
    w4 = 0.0
    try:
        graph_row = (await _db_execute_async_retry(
            db.conn,
            """
            SELECT MAX(entity_link_score)
            FROM transfer_graph
            WHERE wallet_address = ?
            """,
            (wallet_lc,),
        )).fetchone()
        if graph_row and graph_row[0] is not None:
            w4 = max(0.0, min(1.0, float(graph_row[0])))
    except (sqlite3.OperationalError, sqlite3.DatabaseError, ValueError) as exc:
        # Table not yet created or address not yet in graph — silent fallback to 0.0
        logger.debug("[SE][INSIDER] w4 graph fallback addr=%s: %s", wallet_lc[:10], exc)

    # ── NQ-1 weighted blend ───────────────────────────────────────────────
    score = (
        0.30 * w1
        + 0.25 * w2
        + 0.20 * w3
        + 0.15 * w4  # D171 Q2-A
        + 0.10 * w5
    )
    score = max(0.0, min(1.0, score))

    # Log only when graph data contributed meaningfully (for soak observability)
    if w4 > 0.0 or w3 > 0.0 or w5 > 0.0:
        logger.debug(
            "[SE][INSIDER] addr=%s w1=%.3f w2=%.3f w3=%.3f w4=%.3f w5=%.3f → %.3f",
            wallet_lc[:10], w1, w2, w3, w4, w5, score,
        )

    return score if score > 0.0 else None


def _build_friction_snapshot() -> FrictionSnapshot:
    """
    Return a conservative friction snapshot without starting a worker thread.
    Uses GlobalFrictionState for defaults when available.

    Shadow Mode: When not in LIVE_TRADING, uses shadow-calibrated kyle_lambda
    (0.00001 vs system overestimate of 0.001) to allow more signals through.
    """
    import os as _os

    live = _os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes")
    try:
        from panopticon_py.friction_state import GlobalFrictionState

        snap = GlobalFrictionState().get()
        if not live:
            # Shadow mode: use calibrated kyle_lambda
            snap = replace(snap, kyle_lambda=_SHADOW_KYLE_LAMBDA)
        return snap
    except Exception:
        now = time.time()
        kyle = _SHADOW_KYLE_LAMBDA if not live else 0.001
        return FrictionSnapshot(
            network_ping_ms=120.0,
            current_base_fee=0.0015,
            kyle_lambda=kyle,
            gas_cost_estimate=0.25,
            api_health="ok",
            l2_timeout_ms=0.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=now,
        )


async def _collect_insider_sources(
    market_id: str,
    lookback_sec: int,
    db: ShadowDB,
    series_id: str = "",
) -> list[float]:
    """
    D160-2/3: Async-safe collection with ``_effective_insider_threshold()`` per run mode.
    D74: For T1 rolling-window series, aggregate across ALL windows in the series.
    """
    try:
        from datetime import timedelta

        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)
        cutoff_ts = cutoff_dt.isoformat()
    except Exception:
        cutoff_ts = _utc()

    resolved_series_id = series_id
    if not resolved_series_id:
        row = (await _db_execute_async_retry(
            db.conn,
            "SELECT series_id FROM series_members WHERE token_id=? LIMIT 1",
            (market_id,),
        )).fetchone()
        if row:
            resolved_series_id = row[0]

    use_series_agg = False
    if resolved_series_id:
        series_row = (await _db_execute_async_retry(
            db.conn,
            "SELECT series_type FROM event_series WHERE series_id=? LIMIT 1",
            (resolved_series_id,),
        )).fetchone()
        if series_row and series_row[0] == "ROLLING_WINDOW":
            use_series_agg = True

    if use_series_agg:
        all_token_ids: list[str] = [
            r[0] for r in (await _db_execute_async_retry(
                db.conn,
                "SELECT token_id FROM series_members WHERE series_id=?",
                (resolved_series_id,),
            )).fetchall()
        ]
        if len(all_token_ids) > 1:
            placeholders = ",".join(["?"] * len(all_token_ids))
            rows = (await _db_execute_async_retry(
                db.conn,
                f"""
                SELECT DISTINCT wo.address
                FROM wallet_observations wo
                WHERE wo.market_id IN ({placeholders})
                  AND wo.ingest_ts_utc >= ?
                  AND wo.address != '0x0000000000000000000000000000000000000000'
                ORDER BY wo.ingest_ts_utc DESC
                LIMIT 100
                """,
                (*all_token_ids, cutoff_ts),
            )).fetchall()
            logger.info(
                "[D74][SERIES_AGG] series=%s members=%d collected=%d",
                resolved_series_id, len(all_token_ids), len(rows),
            )
        else:
            rows = (await _db_execute_async_retry(
                db.conn,
                """
                SELECT DISTINCT wo.address
                FROM wallet_observations wo
                WHERE wo.market_id = ?
                  AND wo.ingest_ts_utc >= ?
                  AND wo.address != '0x0000000000000000000000000000000000000000'
                ORDER BY wo.ingest_ts_utc DESC
                LIMIT 100
                """,
                (market_id, cutoff_ts),
            )).fetchall()
    else:
        rows = (await _db_execute_async_retry(
            db.conn,
            """
            SELECT DISTINCT wo.address
            FROM wallet_observations wo
            WHERE wo.market_id = ?
              AND wo.ingest_ts_utc >= ?
              AND wo.address != '0x0000000000000000000000000000000000000000'
            ORDER BY wo.ingest_ts_utc DESC
            LIMIT 100
            """,
            (market_id, cutoff_ts),
        )).fetchall()

    sources: list[float] = []
    threshold = _effective_insider_threshold()
    snapshot_hits = 0
    fallback_hits = 0
    for (wallet,) in rows:
        wallet_lower = wallet.lower()
        score = await _get_insider_score(wallet_lower, db)

        # D161-1: Early threshold filter — no point checking source if score is None or below threshold
        if score is None or score < threshold:
            continue

        # Determine source classification without redundant re-query
        # If wallet exists in insider_score_snapshots → snapshot hit, else fallback
        snapshot_check = (await _db_execute_async_retry(
            db.conn,
            "SELECT 1 FROM insider_score_snapshots WHERE address=? LIMIT 1",
            (wallet_lower,),
        )).fetchone()

        if snapshot_check is not None:
            snapshot_hits += 1
        else:
            fallback_hits += 1

        sources.append(score)

    logger.info(
        "[D73_SOURCE_BREAKDOWN] market=%s series=%s snapshot_hits=%d fallback_hits=%d final=%d",
        str(market_id)[:20] if market_id else "None",
        str(resolved_series_id)[:20] if resolved_series_id else "none",
        snapshot_hits,
        fallback_hits,
        len(sources),
    )

    return sources


# ---------------------------------------------------------------------------
# New helpers
# ---------------------------------------------------------------------------


def submit_path_a_alert(market_id: str, z: float, confidence: float) -> Optional[Alert]:
    # AQ-7: z==0 boundary skip
    if z == 0.0:
        logger.debug("[L4_SKIP_ZERO_Z] market=%s z=0.0 dropping", market_id)
        return None
    # AQ-7 default — verify
    direction = "NO" if z < 0 else "YES"
    try:
        alert = Alert(
            source="PATH_A",
            market_id=market_id,
            direction=direction,
            confidence=min(max(confidence, 0.0), 1.0),
            raw_z=z,
        )
    except ValueError as exc:
        logger.error("[D170][PATH_A_ALERT_ERROR] %s market=%s z=%s", exc, market_id, z)
        return None
    return _l4_fuser.submit(alert)


def submit_path_b_alert(
    market_id: str,
    wallet: str,
    side: str,
    confidence: float,
    db: "ShadowDB | None" = None,
) -> Optional[Alert]:
    """
    D171 Q3-A: PATH-B alert with 24h hysteresis.
    Fires only when insider_score crosses threshold from below for the first time,
    and no alert has been emitted for this wallet in the last 24 hours.
    Persists alert_emitted_ts_utc in wallet_watchlist after each alert.
    """
    wallet_lc = wallet.lower()

    # ── 24h hysteresis check via wallet_watchlist.alert_emitted_ts_utc ────
    if db is not None:
        try:
            row = db.conn.execute(
                """
                SELECT alert_emitted_ts_utc FROM wallet_watchlist
                WHERE wallet_address = ? AND alert_emitted_ts_utc IS NOT NULL
                """,
                (wallet_lc,),
            ).fetchone()
            if row and row[0]:
                try:
                    last_alert_ts = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                    now_ts = datetime.now(timezone.utc)
                    elapsed_h = (now_ts - last_alert_ts).total_seconds() / 3600.0
                    if elapsed_h < 24.0:
                        logger.debug(
                            "[D171][PATH_B_HYSTERESIS] wallet=%s last_alert=%.1fh ago — suppressing",
                            wallet_lc[:10], elapsed_h,
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # malformed timestamp — allow alert
        except Exception as exc:
            logger.debug("[D171][PATH_B_HYSTERESIS_ERR] %s", exc)
        # Record alert emission
        try:
            db.conn.execute(
                """
                UPDATE wallet_watchlist
                SET alert_emitted_ts_utc = ?
                WHERE wallet_address = ?
                """,
                (utc_now_rfc3339_ms(), wallet_lc),
            )
            db.conn.commit()
        except Exception as exc:
            logger.warning("[D171][PATH_B_EMIT_ERR] %s", exc)

    # ── Build and submit the alert ────────────────────────────────────────
    direction = str(side).upper()
    if direction not in ("YES", "NO"):
        direction = "YES" if confidence > 0.5 else "NO"
    try:
        alert = Alert(
            source="PATH_B",
            market_id=str(market_id),
            direction=direction,
            confidence=min(max(confidence, 0.0), 1.0),
            raw_z=0.0,
            raw_wallet=wallet_lc,
        )
        logger.info(
            "[D171][PATH_B_ALERT] market=%s wallet=%s side=%s confidence=%.3f",
            str(market_id)[:20], wallet_lc[:10], direction, confidence,
        )
        return alert
    except (ValueError, TypeError) as exc:
        logger.error("[D171][PATH_B_ALERT_ERROR] %s", exc)
        return None






# ---------------------------------------------------------------------------
# Core async processing
# ---------------------------------------------------------------------------


async def _process_event(event: SignalEvent, db: ShadowDB) -> None:
    """
    Process a single SignalEvent through the consensus Bayesian pipeline.
    Writes ONLY to execution_records — never touches wallet_market_positions or paper_trades.
    """
    mc = _mc()
    if mc is not None:
        mc.on_l2_eval()

    z = event.z
    asset_short = str(event.token_id or event.market_id or "")[:14]
    _record_z_and_maybe_flush(asset_short, z)
    market_id = event.market_id

    effective_z, is_dry_run_forced = _maybe_dry_run_override(z)
    if is_dry_run_forced:
        _flush_z_distribution()

    # D107-2/D108: Source validation — must match execution_records CHECK constraint
    safe_source = event.source if event.source in _VALID_EXECUTION_SOURCES else "radar"
    if event.source not in _VALID_EXECUTION_SOURCES:
        logger.warning(
            "[SE] Unknown source=%r for market=%s — defaulting to 'radar'",
            event.source, market_id
        )

    # 1. Z-score threshold check — skip if |z| is below threshold magnitude
    # Threshold is negative (e.g. -4.0), magnitude is abs(threshold)=4.0
    # Skip when |z| < 4.0 (low magnitude), continue when |z| >= 4.0 (high magnitude signal)
    if abs(effective_z) < abs(MIN_ENTROPY_Z_THRESHOLD):
        logging.debug(
            "[SE] |z|=%.2f below threshold magnitude %.2f, skipping",
            abs(effective_z),
            abs(MIN_ENTROPY_Z_THRESHOLD),
        )
        # D158-4: was silent return — persist reject so dashboards / audits see L2 entry
        decision_id = str(uuid4())
        db.append_execution_record({
            "execution_id": decision_id,
            "decision_id": decision_id,
            "accepted": 0,
            "reason": REASON_Z_MAGNITUDE_BELOW_THRESHOLD,
            "mode": "PAPER",
            "source": safe_source,
            "gate_reason": REASON_Z_MAGNITUDE_BELOW_THRESHOLD,
            "latency_ms": 25.0,
            "posterior": 0.0,
            "p_adj": 0.0,
            "qty": 0.0,
            "ev_net": 0.0,
            "avg_entry_price": 0.0,
            "created_ts_utc": _utc(),
            "market_id": market_id,
            "market_tier": event.market_tier,
            "asset_id": event.token_id,
        })
        return

    # D96-C: T1 short-circuit — T1 markets go to Kyle λ path only, not consensus
    if event.market_tier == "t1":
        logging.debug("[SE][T1_SKIP] market=%s z=%.2f — kyle_path only", market_id, effective_z)
        # D121 FIX: Write REJECT record so frontend can see T1 activity
        decision_id = str(uuid4())
        db.append_execution_record({
            "execution_id": decision_id,
            "decision_id": decision_id,
            "accepted": 0,
            "reason": REASON_T1_SHORT_CIRCUIT,
            "mode": "PAPER",
            "source": safe_source,
            "gate_reason": REASON_T1_SHORT_CIRCUIT,
            "latency_ms": 50.0,  # T1 is fast path
            "posterior": 0.0,
            "p_adj": 0.0,
            "qty": 0.0,
            "ev_net": 0.0,
            "avg_entry_price": 0.0,
            "created_ts_utc": _utc(),
            "market_id": market_id,
            "market_tier": event.market_tier,
        })
        return

    # ── D170: L4 Signal Fusion — PATH-A Alert gate ──────────────────────────
    if not is_dry_run_forced:
        threshold_mag = abs(MIN_ENTROPY_Z_THRESHOLD) or 1.0
        l4_confidence_a = min(abs(effective_z) / threshold_mag, 1.0)
        l4_fused = submit_path_a_alert(market_id, effective_z, l4_confidence_a)
        if l4_fused is None:
            l4_decision_id = str(uuid4())
            try:
                db.append_execution_record({
                    "execution_id": l4_decision_id,
                    "decision_id": l4_decision_id,
                    "accepted": 0,
                    "reason": "L4_SUPPRESSED",
                    "mode": "PAPER",
                    "source": safe_source,
                    "gate_reason": "L4_SUPPRESSED",
                    "latency_ms": 5.0,
                    "posterior": 0.0,
                    "p_adj": 0.0,
                    "qty": 0.0,
                    "ev_net": 0.0,
                    "avg_entry_price": 0.0,
                    "created_ts_utc": _utc(),
                    "market_id": market_id,
                    "market_tier": event.market_tier,
                    "asset_id": event.token_id,
                })
            except Exception as l4_db_exc:
                logger.warning("[D170][L4_DB_WRITE_ERR] %s", l4_db_exc)
            return

    # 3. OFI source: orchestrator already mapped HL → PM via OFI_MARKET_MAP
    #    Log for observability only — market_id is already correct
    if event.source == "ofi":
        logging.info("[SE][OFI] ofi=%.3f market=%s", event.ofi_shock_value, market_id)

    # 4. Collect insider sources
    sources = await _collect_insider_sources(market_id, ENTROPY_LOOKBACK_SEC, db, event.series_id)

    # D96-NEW-1c: Grace period — pre-fire / on-fire poll may still be writing wallet_observations
    if len(sources) < MIN_CONSENSUS_SOURCES:
        GRACE_PERIOD_SEC   = 8.0
        RETRY_INTERVAL_SEC = 1.0
        elapsed = 0.0

        while elapsed < GRACE_PERIOD_SEC:
            await asyncio.sleep(RETRY_INTERVAL_SEC)
            elapsed += RETRY_INTERVAL_SEC
            sources = await _collect_insider_sources(market_id, ENTROPY_LOOKBACK_SEC, db, event.series_id)
            logging.debug(
                "[SE][GRACE] market=%s elapsed=%.1fs sources=%d",
                market_id, elapsed, len(sources)
            )
            if len(sources) >= MIN_CONSENSUS_SOURCES:
                logging.info(
                    "[SE][GRACE_PASS] market=%s found %d sources after %.1fs",
                    market_id, len(sources), elapsed
                )
                break

    # ── D81: Transfer Entropy — O(1) bool read [Invariant 4.2] ─────────────
    # [Invariant 4.2] TE only contributes n=1 (bool→int) to consensus denominator.
    # [Invariant 6.2] TE float must NEVER be used as continuous LR input.
    te_cache = _te_cache()
    te_n = 1 if (te_cache is not None and te_cache.is_significant) else 0
    effective_sources = len(sources) + te_n

    tier_lc = (event.market_tier or "").strip().lower()
    bypass_tiers = _insider_bypass_tiers_effective()
    consensus_bypass_active = (
        tier_lc in bypass_tiers
        and effective_sources < MIN_CONSENSUS_SOURCES
    )
    if consensus_bypass_active:
        logger.info(
            "[INSIDER_BYPASS] tier=%s market=%s — insufficient-consensus gate bypassed (PAPER)",
            event.market_tier,
            str(market_id)[:28] if market_id else "None",
        )

    # D167: one-shot dry-run should continue pipeline even when consensus sources are absent.
    if is_dry_run_forced and effective_sources < MIN_CONSENSUS_SOURCES:
        logger.warning(
            "[DRY_RUN_FIRE] bypassing insufficient consensus market=%s sources=%d need=%d",
            market_id,
            effective_sources,
            MIN_CONSENSUS_SOURCES,
        )
        consensus_bypass_active = True
        if not sources:
            sources = [0.5]
        effective_sources = max(effective_sources, MIN_CONSENSUS_SOURCES)

    # 5. Consensus check
    if effective_sources < MIN_CONSENSUS_SOURCES and not consensus_bypass_active:
        decision_id = str(uuid4())
        execution_id = decision_id
        db.append_execution_record({
            "execution_id": execution_id,
            "decision_id": decision_id,
            "accepted": 0,
            "reason": REASON_INSUFFICIENT_CONSENSUS,
            "mode": "PAPER",
            "source": safe_source,  # D107-2: validated against CHECK constraint
            "gate_reason": REASON_INSUFFICIENT_CONSENSUS,
            "latency_ms": 150.0,
            "posterior": 0.0,
            "p_adj": 0.0,
            "qty": 0.0,
            "ev_net": 0.0,
            "avg_entry_price": 0.0,
            "created_ts_utc": _utc(),
            "market_id": market_id,
            "market_tier": event.market_tier,  # D107: was missing — all tiers now recorded
        })
        logging.debug("[SE] market=%s insufficient consensus %d < %d",
                      market_id, effective_sources, MIN_CONSENSUS_SOURCES)
        return

    # 5. Bayesian update
    posterior, lr = _consensus_bayesian_update(sources, extra_sources=te_n)

    # 6. READ wallet_market_positions for LIFO cost basis (READ ONLY!)
    position = db.get_wallet_market_position(event.trigger_address, market_id)
    prev_avg_entry = float(position["avg_entry_price"]) if position else 0.0

    # 7. Get current price AND best ask (D64 Q1: entry = CLOB /book asks[0].price)
    token_id = event.token_id
    current_price = _get_current_price(market_id, token_id, db)
    if current_price is None and not is_dry_run_forced:
        decision_id = str(uuid4())
        execution_id = decision_id
        db.append_execution_record({
            "execution_id": execution_id,
            "decision_id": decision_id,
            "accepted": 0,
            "reason": REASON_NO_PRICE_DATA,
            "mode": "PAPER",
            "source": safe_source,  # D107-2: validated against CHECK constraint
            "gate_reason": REASON_NO_PRICE_DATA,
            "latency_ms": 150.0,
            "posterior": posterior,
            "p_adj": 0.0,
            "qty": 0.0,
            "ev_net": 0.0,
            "avg_entry_price": prev_avg_entry,
            "created_ts_utc": _utc(),
            "market_id": market_id,
            "market_tier": event.market_tier,  # D107: was missing — all tiers now recorded
        })
        logging.warning("[SE] market=%s no price data", market_id)
        return
    elif current_price is None and is_dry_run_forced:
        current_price = 0.5
        logger.warning("[DRY_RUN_FIRE] using synthetic current_price=0.5 market=%s", market_id)

    # 7.1: Fetch best ask for entry price (D64 Q1 ruling)
    # If no asks available → NO_TRADE (do not use 0.5 fallback)
    best_ask = fetch_best_ask(token_id) if token_id else None
    if best_ask is None and not is_dry_run_forced:
        decision_id = str(uuid4())
        execution_id = decision_id
        db.append_execution_record({
            "execution_id": execution_id,
            "decision_id": decision_id,
            "accepted": 0,
            "reason": REASON_NO_PRICE_DATA,
            "mode": "PAPER",
            "source": safe_source,  # D107-2: validated against CHECK constraint
            "gate_reason": REASON_NO_PRICE_DATA,
            "latency_ms": 150.0,
            "posterior": posterior,
            "p_adj": 0.0,
            "qty": 0.0,
            "ev_net": 0.0,
            "avg_entry_price": prev_avg_entry,
            "created_ts_utc": _utc(),
            "market_id": market_id,
            "market_tier": event.market_tier,  # D107: was missing — all tiers now recorded
        })
        logger.info("[SE][ENTRY_PRICE] market=%s no asks available, skipping trade", market_id)
        return
    elif best_ask is None and is_dry_run_forced:
        best_ask = 0.5
        logger.warning("[DRY_RUN_FIRE] using synthetic best_ask=0.5 market=%s", market_id)

    # D101: T2-POL political market logging — no posterior override
    # Political markets use full Bayesian consensus (same as standard T2).
    # market_tier="t2_pol" is recorded for metrics tracking.
    if event.market_tier == "t2_pol":
        logger.info(
            "[SE][T2_POL] political market=%s posterior=%.3f sources=%d",
            str(market_id)[:20] if market_id else "None",
            posterior,
            len(sources),
        )

    # 7.5 T5 Sports override: no financial insider signal in sports markets
    # Use conservative 50/50 base rate (p_prior = 0.50) instead of Bayesian posterior
    # D118: Apply time-to-event decay weighting — closer to event = stronger signal
    # D119: Use DEBUG level for passive monitoring; use grep "[SE][T5_PRIOR]" to observe
    if event.market_tier == "t5":
        tte = event.time_to_event
        if tte > 0:
            weight = _t5_time_decay_weight(time.time() + tte)
            tte_h = tte / 3600.0
        else:
            weight = 0.30  # unknown expiry → conservative minimum
            tte_h = -1.0
        posterior = 0.50 * weight
        logger.debug(
            "[SE][T5_PRIOR] market=%s tte_h=%.1f weight=%.2f prior=%.4f",
            market_id, tte_h, weight, posterior,
        )

    # 8. Build FastSignalInput
    # Shadow mode: lower order_size to reduce slippage while kyle_lambda is recalibrated
    live_trading = os.getenv("LIVE_TRADING", "").lower() in ("1", "true", "yes")
    if live_trading:
        order_size_usd = DEFAULT_CAPITAL * KELLY_FRACTION  # 25.0 USD
        slip_tol = 0.009
    else:
        order_size_usd = _SHADOW_ORDER_SIZE_USD  # 10.0 USD (shadow mode)
        slip_tol = _SHADOW_SLIPPAGE_TOLERANCE  # 0.05 (5%, shadow mode)
    signal_input = FastSignalInput(
        p_prior=posterior,
        quote_price=current_price,
        payout=1.0,
        capital_in=current_price * order_size_usd,
        order_size=order_size_usd,
        avg_entry_price=best_ask,
        delta_t_ms=150.0,
        gamma=0.001,
        slippage_tolerance=slip_tol,
        min_ev_threshold=0.0,
        daily_opp_cost=0.0008,
        days_to_resolution=3.0,
        bid_ask_imbalance=0.0,
    )

    # 9. L4 Fast Gate
    snapshot = _build_friction_snapshot()
    if mc is not None:
        mc.on_l3_eval()
    gate = fast_execution_gate(signal_input, snapshot)

    # P1 DIAG: Log FastSignalInput parameters for every gate call (regardless of decision)
    # Do NOT modify business logic — this is read-only diagnostic instrumentation
    logger.info(
        "[DIAG][FAST_SIGNAL_INPUT] decision=%s reason=%s ev_net=%.4f | "
        "p_prior=%.4f posterior=%.4f p_adj=%.4f quote_price=%.4f payout=%.4f "
        "capital_in=%.4f order_size=%.4f avg_entry=%.4f delta_t_ms=%.2f "
        "gamma=%.6f daily_opp_cost=%.6f days_to_res=%.1f",
        gate.decision.name,
        gate.reason,
        gate.ev_net,
        signal_input.p_prior,
        posterior,
        gate.p_adjusted,
        signal_input.quote_price,
        signal_input.payout,
        signal_input.capital_in,
        signal_input.order_size,
        signal_input.avg_entry_price,
        signal_input.delta_t_ms,
        signal_input.gamma,
        signal_input.daily_opp_cost,
        signal_input.days_to_resolution,
    )

    if gate.decision == GateDecision.ABORT:
        reason = gate.reason
        action = "HOLD"
        accepted = 0
    elif gate.decision == GateDecision.DEGRADE:
        reason = f"{REASON_KELLY_DEGRADED_PREFIX}{gate.reason}"
        action = "BUY"
        accepted = 1
    else:
        reason = gate.reason
        action = "BUY"
        accepted = 1

    if is_dry_run_forced and accepted == 0:
        logger.warning(
            "[DRY_RUN_FIRE] overriding gate decision=%s to accepted BUY for pipeline validation",
            gate.decision.name,
        )
        reason = "DRY_RUN_FORCED_ACCEPT"
        action = "BUY"
        accepted = 1

    # ── MetricsCollector hook (in-process, no DB writes in hot path) ──────────
    if mc is not None:
        mc.on_gate_result(accepted=bool(accepted), ev=gate.ev_net)
        mc.on_signal_queued(
            depth=0,  # queue depth not meaningful here
            tier=event.market_tier,
            p_posterior=posterior,
            z=effective_z,  # D167: includes one-shot dry-run override if enabled
        )

    # 10. Write execution_record (INSERT — gate decision, pre-CLOB)
    decision_id = str(uuid4())
    execution_id = decision_id  # Option A: unified ID, one signal → one decision → one record
    gate_reason_out = (
        REASON_INSIDER_BYPASS_PAPER if consensus_bypass_active else gate.reason
    )
    db.append_execution_record({
        "execution_id": execution_id,
        "decision_id": decision_id,
        "accepted": accepted,
        "reason": reason,
        "mode": "PAPER",
        "source": safe_source,  # D107-2: validated against CHECK constraint
        "gate_reason": gate_reason_out,
        "latency_ms": signal_input.delta_t_ms,
        "posterior": posterior,
        "p_adj": gate.p_adjusted,
        "qty": signal_input.order_size,
        "ev_net": gate.ev_net,
        "avg_entry_price": signal_input.avg_entry_price,
        "created_ts_utc": _utc(),
        "market_id": market_id,
        "market_tier": event.market_tier,  # D107: was missing — all tiers now recorded
    })

    if accepted:
        global _SIGNAL_FIRED_COUNT
        _SIGNAL_FIRED_COUNT += 1
        logger.info(
            "[SIGNAL_FIRED] asset=%s market=%s z=%.3f dry_run=%s count=%d",
            asset_short or "unknown",
            str(market_id)[:20] if market_id else "None",
            effective_z,
            is_dry_run_forced,
            _SIGNAL_FIRED_COUNT,
        )

    # D103-1: Record last signal timestamp for accepted T2-POL signals
    if accepted and event.market_tier == "t2_pol":
        try:
            db.update_pol_last_signal_ts(market_id, utc_now_rfc3339_ms())
        except Exception as _e:
            logger.warning("[POL] last_signal_ts update failed: %s", _e)

    # 11. CLOB submission — only on PASS or DEGRADE (gate.decision != ABORT)
    if gate.decision != GateDecision.ABORT:
        snapshot = _build_friction_snapshot()
        clob_result = await submit_fok_order(
            market_id=market_id,
            token_id=token_id,
            side="BUY",
            size=signal_input.order_size,
            price=signal_input.quote_price,
            decision_id=decision_id,
            private_key=os.getenv("CLOB_SIGNER_PRIVATE_KEY", ""),
            state=None,  # GlobalFrictionState not started in signal_engine subprocess
            dry_run=os.getenv("LIVE_TRADING", "").lower() not in ("1", "true", "yes"),
            timeout_sec=12.0,
        )
        db.update_execution_clob_result(
            execution_id=decision_id,
            clob_order_id=clob_result.clob_order_id,
            tx_hash=clob_result.tx_hash,
            settlement_status="pending_submit" if clob_result.accepted else "rejected",
            reason=clob_result.reason if not clob_result.accepted else None,
        )

    log_msg = (f"[SE][{safe_source}] market={market_id} z={effective_z:.2f} "
               f"sources={len(sources)} posterior={posterior:.3f} "
               f"action={action} reason={reason}")
    if action == "BUY":
        logging.info(log_msg)
    else:
        logging.debug(log_msg)


async def _run_async(queue: asyncio.Queue[SignalEvent], db: ShadowDB) -> None:
    """
    Zero-latency event-driven loop [Invariant 1.1].
    Queue.get() blocks until event arrives (OFI or Radar signal).
    No DB polling fallback — queue is the only signal source.
    """
    _diag_print_ev_config()  # Print EV config once at startup
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            global _DRY_RUN_SYNTHETIC_EMITTED
            if _DRY_RUN_SIGNAL and not _DRY_RUN_SYNTHETIC_EMITTED:
                _DRY_RUN_SYNTHETIC_EMITTED = True
                synthetic_market_id = os.getenv(
                    "PANOPTICON_DRY_RUN_MARKET_ID",
                    "27911616648163853231017805596118911526202185567944724228908312975649746722206",
                )
                synthetic_token_id = os.getenv("PANOPTICON_DRY_RUN_TOKEN_ID", synthetic_market_id)
                logger.warning(
                    "[DRY_RUN_FIRE] emitting synthetic SignalEvent market=%s token=%s",
                    synthetic_market_id[:20],
                    synthetic_token_id[:20],
                )
                synthetic_event = SignalEvent(
                    source="radar",
                    market_id=synthetic_market_id,
                    token_id=synthetic_token_id,
                    entropy_z=0.1,  # will be overridden by _maybe_dry_run_override()
                    trigger_address="system",
                    trigger_ts_utc=_utc(),
                    market_tier="t3",
                )
                await _process_event(synthetic_event, db)
                mc = _mc()
                if mc is not None:
                    mc.on_signal_processed()
                continue
            # No DB fallback — queue is the only signal path [Invariant 1.1]
            mc = _mc()
            if mc is not None:
                mc.on_signal_queued(depth=queue.qsize(), tier="", p_posterior=None, z=None)
            continue

        await _process_event(event, db)
        mc = _mc()
        if mc is not None:
            mc.on_signal_processed()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Legacy subprocess entry point (used by start_shadow_hydration.py)."""
    load_repo_env()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    live_trading = os.getenv("LIVE_TRADING", "false").lower() in {"1", "true", "yes"}
    if live_trading:
        raise RuntimeError(
            "LIVE_TRADING must be false — signal engine runs in shadow/paper mode only"
        )

    parser = argparse.ArgumentParser(description="Panopticon Signal Engine")
    parser.add_argument(
        "--db-path",
        default=os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db"),
        help="Path to ShadowDB (default: PANOPTICON_DB_PATH env var or data/panopticon.db)",
    )
    args = parser.parse_args()

    db = ShadowDB(db_path=args.db_path)
    db.bootstrap()

    logging.info(
        "Signal engine starting — zero-latency mode min_consensus=%d insider_threshold=%.2f (mode=%s)",
        MIN_CONSENSUS_SOURCES,
        _effective_insider_threshold(),
        "LIVE" if _live_trading_env_on() else "PAPER",
    )

    queue: asyncio.Queue[SignalEvent] = asyncio.Queue()

    async def runner() -> None:
        await _run_async(queue, db)

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logging.info("Signal engine shutting down")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
