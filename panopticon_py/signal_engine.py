"""
Panopticon Signal Engine — zero-latency event-driven consensus Bayesian decision actor (v4-FINAL).

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
import logging
import math

logger = logging.getLogger(__name__)
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from panopticon_py.db import ShadowDB
from panopticon_py.execution.clob_client import submit_fok_order
from panopticon_py.execution.constants import (
    REASON_INSUFFICIENT_CONSENSUS,
    REASON_KELLY_DEGRADED_PREFIX,
    REASON_NO_PRICE_DATA,
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

# D108: Schema-sync constant — must match execution_records CHECK constraint
_VALID_EXECUTION_SOURCES: frozenset[str] = frozenset({"radar", "ofi"})

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


def _get_insider_score(wallet: str, db: ShadowDB) -> float | None:
    """
    Return the most recent insider score for a wallet, or the entity trust score
    as a fallback from discovered_entities.

    Query order (fastest first):
    1. insider_score_snapshots — real-time scoring snapshots
    2. discovered_entities.insider_score — D37 whale scanner injection
    3. tracked_wallets + discovered_entities.trust_score — legacy mapping
    """
    wallet = wallet.lower()
    row = db.conn.execute(
        """
        SELECT score FROM insider_score_snapshots
        WHERE address = ?
        ORDER BY ingest_ts_utc DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    if row is not None:
        return float(row[0])

    # D37: Direct query to discovered_entities.insider_score (whale scanner injection)
    # D101: Wrapped in try/except to guard against pre-migration DB state.
    # Filter score > 0.0 to skip DEFAULT 0.0 entries (not scored entities).
    try:
        row_de = db.conn.execute(
            """
            SELECT insider_score FROM discovered_entities
            WHERE entity_id = ? COLLATE NOCASE
            LIMIT 1
            """,
            (wallet,),
        ).fetchone()
        if row_de is not None and row_de[0] is not None:
            score = float(row_de[0])
            if score > 0.0:  # 0.0 = DEFAULT, not yet scored — skip
                return score
    except Exception as exc:
        logger.debug("[SE][D37_FALLBACK_ERR] %s", exc)

    row2 = db.conn.execute(
        """
        SELECT e.trust_score FROM tracked_wallets tw
        JOIN discovered_entities e ON e.entity_id = tw.entity_id
        WHERE tw.wallet_address = ?
        ORDER BY tw.last_updated_at DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    if row2 is not None:
        return float(row2[0]) / 100.0  # trust_score is 0-100, normalize to 0-1

    return None


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


def _collect_insider_sources(
    market_id: str,
    lookback_sec: int,
    db: ShadowDB,
    series_id: str = "",
) -> list[float]:
    """
    Collect insider scores from wallets that have recent observations for the given market,
    filtered by INSIDER_SCORE_THRESHOLD.

    D74: For T1 rolling-window series (e.g. BTC 5m), aggregate across ALL windows
    in the series by resolving market_id -> series_id -> all series_members.
    BTC 5m slug changes every 5min but the underlying event/series is the same.
    """
    try:
        from datetime import timedelta

        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)
        cutoff_ts = cutoff_dt.isoformat()
    except Exception:
        cutoff_ts = _utc()

    # D74: Resolve series_id from market_id if not provided.
    # For T1 BTC 5m markets, market_id is the condition_id (0x... or numeric).
    # We need to find the series and aggregate across all windows.
    resolved_series_id = series_id
    if not resolved_series_id:
        row = db.conn.execute(
            "SELECT series_id FROM series_members WHERE token_id=? LIMIT 1",
            (market_id,),
        ).fetchone()
        if row:
            resolved_series_id = row[0]  # tuple index, not dict key

    # D74: Check if this is a T1 rolling-window market.
    # If so, collect wallets from ALL series members, not just the current window.
    use_series_agg = False
    if resolved_series_id:
        series_row = db.conn.execute(
            "SELECT series_type FROM event_series WHERE series_id=? LIMIT 1",
            (resolved_series_id,),
        ).fetchone()
        if series_row and series_row[0] == "ROLLING_WINDOW":
            use_series_agg = True

    if use_series_agg:
        # D74: Aggregate across all series members for rolling-window T1 markets.
        # BTC 5m generates a new window every 5min; we want consensus across the
        # entire series, not just the current window.
        all_token_ids: list[str] = [
            r[0] for r in db.conn.execute(
                "SELECT token_id FROM series_members WHERE series_id=?",
                (resolved_series_id,),
            ).fetchall()
        ]
        if len(all_token_ids) > 1:
            placeholders = ",".join(["?"] * len(all_token_ids))
            rows = db.conn.execute(
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
            ).fetchall()
            logger.info(
                "[D74][SERIES_AGG] series=%s members=%d collected=%d",
                resolved_series_id, len(all_token_ids), len(rows),
            )
        else:
            # Fallback to single market if series has only 1 member
            rows = db.conn.execute(
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
            ).fetchall()
    else:
        rows = db.conn.execute(
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
        ).fetchall()

    # D73: Source breakdown — track snapshot hits vs discovered_entities fallback hits
    snapshot_hits = 0
    fallback_hits = 0
    wallet_lower = None

    sources: list[float] = []
    for (wallet,) in rows:
        wallet_lower = wallet.lower()
        score = _get_insider_score(wallet_lower, db)

        # D73: Determine which path returned the score
        # Query snapshot table to check if score came from insider_score_snapshots
        snapshot_row = db.conn.execute(
            "SELECT 1 FROM insider_score_snapshots WHERE address=? AND score>=? LIMIT 1",
            (wallet_lower, INSIDER_SCORE_THRESHOLD),
        ).fetchone()

        if snapshot_row is not None and score is not None and score >= INSIDER_SCORE_THRESHOLD:
            snapshot_hits += 1
            sources.append(score)
        elif score is not None and score >= INSIDER_SCORE_THRESHOLD:
            # Score came from discovered_entities fallback (whale scanner)
            fallback_hits += 1
            sources.append(score)

    # D73: Log source breakdown for diagnostic verification
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






# ---------------------------------------------------------------------------
# Core async processing
# ---------------------------------------------------------------------------


async def _process_event(event: SignalEvent, db: ShadowDB) -> None:
    """
    Process a single SignalEvent through the consensus Bayesian pipeline.
    Writes ONLY to execution_records — never touches wallet_market_positions or paper_trades.
    """
    z = event.z
    market_id = event.market_id

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
    if abs(z) < abs(MIN_ENTROPY_Z_THRESHOLD):
        logging.debug("[SE] |z|=%.2f below threshold magnitude %.2f, skipping", abs(z), abs(MIN_ENTROPY_Z_THRESHOLD))
        return

    # D96-C: T1 short-circuit — T1 markets go to Kyle λ path only, not consensus
    if event.market_tier == "t1":
        logging.debug("[SE][T1_SKIP] market=%s z=%.2f — kyle_path only", market_id, z)
        return

    # 3. OFI source: orchestrator already mapped HL → PM via OFI_MARKET_MAP
    #    Log for observability only — market_id is already correct
    if event.source == "ofi":
        logging.info("[SE][OFI] ofi=%.3f market=%s", event.ofi_shock_value, market_id)

    # 4. Collect insider sources
    sources = _collect_insider_sources(market_id, ENTROPY_LOOKBACK_SEC, db, event.series_id)

    # D96-NEW-1c: Grace period — pre-fire / on-fire poll may still be writing wallet_observations
    if len(sources) < MIN_CONSENSUS_SOURCES:
        GRACE_PERIOD_SEC   = 8.0
        RETRY_INTERVAL_SEC = 1.0
        elapsed = 0.0

        while elapsed < GRACE_PERIOD_SEC:
            await asyncio.sleep(RETRY_INTERVAL_SEC)
            elapsed += RETRY_INTERVAL_SEC
            sources = _collect_insider_sources(market_id, ENTROPY_LOOKBACK_SEC, db, event.series_id)
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

    # 5. Consensus check
    if effective_sources < MIN_CONSENSUS_SOURCES:
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
    if current_price is None:
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

    # 7.1: Fetch best ask for entry price (D64 Q1 ruling)
    # If no asks available → NO_TRADE (do not use 0.5 fallback)
    best_ask = fetch_best_ask(token_id) if token_id else None
    if best_ask is None:
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
    if event.market_tier == "t5":
        posterior = 0.50
        logger.info("[SE][T5_SPORTS] sports market=%s p_prior overridden to 0.50", market_id)

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

    # ── MetricsCollector hook (in-process, no DB writes in hot path) ──────────
    mc = _mc()
    if mc is not None:
        mc.on_gate_result(accepted=bool(accepted), ev=gate.ev_net)
        mc.on_signal_queued(
            depth=0,  # queue depth not meaningful here
            tier=event.market_tier,
            p_posterior=posterior,
            z=event.z,  # canonical score — handles entropy_z=None case
        )

    # 10. Write execution_record (INSERT — gate decision, pre-CLOB)
    decision_id = str(uuid4())
    execution_id = decision_id  # Option A: unified ID, one signal → one decision → one record
    db.append_execution_record({
        "execution_id": execution_id,
        "decision_id": decision_id,
        "accepted": accepted,
        "reason": reason,
        "mode": "PAPER",
        "source": safe_source,  # D107-2: validated against CHECK constraint
        "gate_reason": gate.reason,
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

    log_msg = (f"[SE][{safe_source}] market={market_id} z={z:.2f} "
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
        "Signal engine starting — zero-latency mode min_consensus=%d insider_threshold=%.2f",
        MIN_CONSENSUS_SOURCES,
        INSIDER_SCORE_THRESHOLD,
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
