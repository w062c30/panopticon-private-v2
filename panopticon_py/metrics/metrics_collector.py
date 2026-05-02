"""
Panopticon MetricsCollector — in-process RVF metrics aggregator.

Hooked into existing log calls and in-process signals.
No DB reads in hot path (Invariant 4.2).

Metrics are:
  - Aggregated every 1s by collect()
  - Written to rvf_metrics_snapshots DB table every 5s by persist()
  - Pushed via FastAPI WebSocket every 5s by the API server
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from panopticon_py.metrics.metrics_schema import (
    ConsensusStats,
    GateStats,
    GoLiveSnapshot,
    KyleStats,
    MetricsSnapshot,
    QueueStats,
    ReadinessSnapshot,
    SeriesStats,
    WindowStats,
    WsStats,
)

logger = logging.getLogger(__name__)

# ── Singleton factory ──────────────────────────────────────────────────────────

_collector: "MetricsCollector | None" = None


def get_collector() -> "MetricsCollector":
    global _collector
    if _collector is None:
        _collector = MetricsCollector()
    return _collector


# ── Rate counter helper ───────────────────────────────────────────────────────

class _RateCounter:
    """Tracks counts in a rolling time window."""
    __slots__ = ("_window_sec", "_events", "_last_add")

    def __init__(self, window_sec: float = 60.0) -> None:
        self._window_sec = window_sec
        self._events: deque[float] = deque()
        self._last_add: float = 0.0

    def add(self, t: float | None = None) -> None:
        if t is None:
            t = time.time()
        self._events.append(t)
        self._last_add = t

    def count(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        cutoff = now - self._window_sec
        while self._events and self._events[0] < cutoff:
            self._events.popleft()
        return len(self._events)


# ── Main collector ────────────────────────────────────────────────────────────

class MetricsCollector:
    """
    In-process RVF metrics collector.

    Metrics are accumulated continuously from hook calls.
    collect() computes a MetricsSnapshot (1s interval).
    persist() writes to DB (5s interval).

    Usage::

        mc = get_collector()
        mc.on_l1_subscription(t1=3, t2=5, t3=192, t5=0)
        mc.on_kyle_compute(asset_id="0x...", lambda_obs=0.0001)
        mc.on_gate_result(accepted=True)
        snapshot = mc.collect()
    """

    def __init__(self) -> None:
        self._started_at = time.time()

        # ── L1 WS stats ────────────────────────────────────────────────────────
        self._ws_connected = False
        self._t1 = self._t2 = self._t3 = self._t5 = 0
        self._trade_ticks_60s = _RateCounter(60.0)
        self._real_trade_ticks_60s = _RateCounter(60.0)  # D131: Debt-5 upper-bound proxy
        self._book_events_60s = _RateCounter(60.0)
        self._current_t1_window_start = 0
        self._current_t1_window_end = 0
        self._secs_remaining = 0.0
        self._t1_rollover_count = 0
        self._last_rollover_log_ts = 0.0
        self._last_ws_msg_ts = 0.0

        # D121: WS subscription token tracking
        self._ws_subscribed_tokens = 0
        self._ws_total_tokens = 0
        self._ws_last_payload_bytes = 0

        # ── Kyle stats ─────────────────────────────────────────────────────────
        self._kyle_samples: deque[tuple[float, str, float]] = deque()  # (ts, asset_id, lambda)
        self._kyle_compute_rc = _RateCounter(60.0)
        self._kyle_skip_rc = _RateCounter(60.0)
        self._last_kyle_compute_ts = 0.0
        self._last_kyle_status: str = "none"

        # ── Window stats ────────────────────────────────────────────────────────
        self._active_entropy_windows = 0
        self._last_cleanup_count = 0
        self._last_cleanup_ts = 0.0
        # D37 FIX: Track entropy fires (active_entropy_windows from 5min rolling count)
        self._entropy_fire_rc = _RateCounter(300.0)

        # ── Queue stats ─────────────────────────────────────────────────────────
        self._queue_depth = 0
        self._processed_60s = _RateCounter(60.0)
        self._p_posterior_t1: deque[float] = deque()
        self._p_posterior_t2: deque[float] = deque()
        self._z_t1: deque[float] = deque()
        self._z_t2: deque[float] = deque()
        # D37 FIX: Track signal z-scores from entropy fires for mean_z_t1/t2
        self._signal_z_vals: deque[tuple[float, float]] = deque()  # (ts, z)

        # ── Gate stats ─────────────────────────────────────────────────────────
        self._gate_evaluated_rc = _RateCounter(60.0)
        self._gate_pass_rc = _RateCounter(60.0)
        self._gate_abort_rc = _RateCounter(60.0)
        self._paper_trades_total = 0
        self._paper_win_count = 0
        self._paper_win_rate = 0.0
        self._avg_ev = 0.0

        # ── Series stats ───────────────────────────────────────────────────────
        self._deadline_ladders = 0
        self._rolling_windows = 0
        self._total_series = 0
        self._monotone_violations = 0
        self._last_violation_slug = ""
        self._last_violation_gap = 0.0
        self._catalyst_events_today = 0
        self._oracle_high_risk = 0

        # ── Consensus / wallet stats ──────────────────────────────────────────
        self._qualifying_wallets = 0
        self._new_candidates = 0
        self._path_b_promoted = 0
        self._markets_consensus_ready = 0
        self._markets_consensus_total = 0  # D56: true count without LIMIT
        self._consensus_markets: list = []  # [{slug, wallet_count}] latest 10

        # ── Price debug stats (D50c) ──────────────────────────────────────
        self._price_debug_last_source: str | None = None
        self._price_debug_last_spread: float | None = None
        self._price_debug_no_price_rc = _RateCounter(86400.0)  # 24h rolling window

        # ── D81: Identity coverage + TE stats ───────────────────────────────
        self._coverage_stats: dict = {}
        self._te_stats: dict = {}

    # ── Hooks (called from radar / signal_engine log handlers) ───────────────

    def on_ws_connected(self) -> None:
        self._ws_connected = True
        self._last_ws_msg_ts = time.time()

    def on_ws_disconnected(self) -> None:
        self._ws_connected = False

    def on_ws_subscription_update(self, ws_tokens: int, total_tokens: int, payload_bytes: int) -> None:
        """D121: Track WS subscription token counts and payload size."""
        self._ws_subscribed_tokens = ws_tokens
        self._ws_total_tokens = total_tokens
        self._ws_last_payload_bytes = payload_bytes

    def on_ws_message(self) -> None:
        self._last_ws_msg_ts = time.time()

    def on_l1_subscription(self, t1: int, t2: int, t3: int, t5: int) -> None:
        self._t1 = t1
        self._t2 = t2
        self._t3 = t3
        self._t5 = t5
        if t1 > 0:
            self._ws_connected = True

    def on_trade_tick(self) -> None:
        self._trade_ticks_60s.add()

    def on_real_trade_tick(self) -> None:
        """D131: Debt-5 — real trade event from embedded price or standalone last_trade_price."""
        self._real_trade_ticks_60s.add()

    def on_book_event(self) -> None:
        self._book_events_60s.add()

    def on_t1_window_rollover(self, window_start: int, window_end: int, secs_remaining: float) -> None:
        self._current_t1_window_start = window_start
        self._current_t1_window_end = window_end
        self._secs_remaining = secs_remaining
        self._t1_rollover_count += 1
        logger.debug("[METRICS][T1_ROLLOVER] window_start=%d count=%d", window_start, self._t1_rollover_count)

    def on_kyle_compute(self, asset_id: str, lambda_obs: float) -> None:
        now = time.time()
        self._kyle_samples.append((now, asset_id, lambda_obs))
        self._kyle_compute_rc.add(now)
        self._last_kyle_compute_ts = now
        self._last_kyle_status = "compute"

    def on_kyle_skip(self) -> None:
        now = time.time()
        self._kyle_skip_rc.add(now)
        self._last_kyle_compute_ts = now
        self._last_kyle_status = "skip"

    def on_entropy_window_cleanup(self, removed_count: int, remaining: int) -> None:
        self._active_entropy_windows = remaining
        self._last_cleanup_count = removed_count
        self._last_cleanup_ts = time.time()

    def on_entropy_window_active(self, count: int) -> None:
        # D37 FIX: Track entropy fire rate (active_entropy_windows = fires in last 5min)
        self._entropy_fire_rc.add()
        self._active_entropy_windows = self._entropy_fire_rc.count()

    def on_entropy_fire(self, z: float) -> None:
        """
        D37 FIX: Called when an entropy fire event is detected.
        Records the fire for active_entropy_windows metric and z-score for mean_z_t1.
        """
        now = time.time()
        self._entropy_fire_rc.add(now)
        self._active_entropy_windows = self._entropy_fire_rc.count()
        # Store z-score with timestamp for mean_z calculation
        self._signal_z_vals.append((now, z))
        # Keep only last 5min
        cutoff = now - 300
        while self._signal_z_vals and self._signal_z_vals[0][0] < cutoff:
            self._signal_z_vals.popleft()

    def on_signal_queued(self, depth: int, tier: str, p_posterior: float | None = None, z: float | None = None) -> None:
        self._queue_depth = depth
        if tier == "t1" and p_posterior is not None:
            self._p_posterior_t1.append(p_posterior)
        elif tier in ("t2", "t3") and p_posterior is not None:
            self._p_posterior_t2.append(p_posterior)
        if tier == "t1" and z is not None:
            self._z_t1.append(z)
        elif tier in ("t2", "t3") and z is not None:
            self._z_t2.append(z)

    def on_signal_processed(self) -> None:
        self._processed_60s.add()

    def on_gate_result(self, accepted: bool, ev: float | None = None) -> None:
        now = time.time()
        self._gate_evaluated_rc.add(now)
        if accepted:
            self._gate_pass_rc.add(now)
            self._paper_trades_total += 1
            if ev is not None:
                self._avg_ev = (self._avg_ev * (self._paper_trades_total - 1) + ev) / self._paper_trades_total
        else:
            self._gate_abort_rc.add(now)

    def on_paper_win_rate(self, win_rate: float) -> None:
        self._paper_win_rate = win_rate

    def on_series_update(self, deadline_ladders: int, rolling_windows: int,
                         monotone_violations: int,
                         last_violation_slug: str = "",
                         last_violation_gap: float = 0.0) -> None:
        self._deadline_ladders = deadline_ladders
        self._rolling_windows = rolling_windows
        self._total_series = deadline_ladders + rolling_windows
        self._monotone_violations = monotone_violations
        self._last_violation_slug = last_violation_slug
        self._last_violation_gap = last_violation_gap

    def on_catalyst_event(self) -> None:
        self._catalyst_events_today += 1

    def on_oracle_high_risk(self, count: int) -> None:
        self._oracle_high_risk = count

    def on_price_fetch_result(self, source: str | None, spread: float | None) -> None:
        """
        Record price fetch result for D50c dashboard display.
        source: "mid" | "last_trade" | "no_price"
        spread: float (best_ask - best_bid) or None
        """
        self._price_debug_last_source = source
        self._price_debug_last_spread = spread
        if source == "no_price":
            self._price_debug_no_price_rc.add()

    def sync_series_from_db(self, db) -> None:
        """
        D37 FIX: Query series stats from DB and update metrics.
        Called periodically (e.g. every 60s) from the heartbeat loop.
        Fills deadline_ladders, rolling_windows, monotone_violations.
        """
        try:
            conn = db.conn if hasattr(db, "conn") else db
            now_utc = "utc('now')"

            # D37 FIX: Query series stats from DB
            # rolling_windows = count of distinct T1/T2 series (from series_members)
            rolling = conn.execute("""
                SELECT COUNT(DISTINCT series_id) FROM series_members
                WHERE market_tier IN ('t1', 't2')
            """).fetchone()
            rolling_count = rolling[0] if rolling else 0

            # deadline_ladders = count of DEADLINE_LADDER event_series entries
            deadline = conn.execute("""
                SELECT COUNT(DISTINCT series_id) FROM event_series
                WHERE series_type = 'DEADLINE_LADDER'
            """).fetchone()
            deadline_count = deadline[0] if deadline else 0

            # Count today's catalyst events
            catalysts = conn.execute("""
                SELECT COUNT(*) FROM catalyst_events
                WHERE ts_utc >= date('now', 'utc')
            """).fetchone()
            catalyst_count = catalysts[0] if catalysts else 0

            # Count today's monotone violations
            violations = conn.execute("""
                SELECT COUNT(*) FROM series_violations
                WHERE ts_utc >= date('now', 'utc')
            """).fetchone()
            violation_count = violations[0] if violations else 0

            self._deadline_ladders = deadline_count
            self._rolling_windows = rolling_count
            self._total_series = deadline_count + rolling_count
            self._monotone_violations = violation_count
            self._catalyst_events_today = catalyst_count
        except Exception as exc:
            # D37 FIX: Use warning level so we actually see the error in logs
            logger.warning("[METRICS][SERIES_SYNC] error: %s", exc)

    def sync_consensus_from_db(self, db) -> None:
        """
        Query consensus / wallet readiness stats from DB.
        Called periodically (e.g. every 60s) from the heartbeat loop.
        Fills qualifying_wallets, new_candidates, path_b_promoted, markets_consensus_ready.
        """
        try:
            conn = db.conn if hasattr(db, "conn") else db

            # Import signal engine constants at call time (avoid circular import at module load)
            try:
                from panopticon_py.signal_engine import (
                    ENTROPY_LOOKBACK_SEC,
                    INSIDER_SCORE_THRESHOLD,
                    MIN_CONSENSUS_SOURCES,
                )
            except ImportError:
                ENTROPY_LOOKBACK_SEC = 360
                INSIDER_SCORE_THRESHOLD = 0.55
                MIN_CONSENSUS_SOURCES = 2

            lookback_str = f"-{ENTROPY_LOOKBACK_SEC} seconds"

            # Total qualifying wallets (discovered_entities with score >= threshold)
            row_total = conn.execute("""
                SELECT COUNT(*) FROM discovered_entities
                WHERE insider_score >= ?
            """, (INSIDER_SCORE_THRESHOLD,)).fetchone()
            self._qualifying_wallets = row_total[0] if row_total else 0

            # New candidates: qualifying wallets WITHOUT path_b_promoted tag
            row_new = conn.execute("""
                SELECT COUNT(*) FROM discovered_entities
                WHERE insider_score >= ?
                  AND (primary_tag IS NULL OR primary_tag != 'path_b_promoted')
            """, (INSIDER_SCORE_THRESHOLD,)).fetchone()
            self._new_candidates = row_new[0] if row_new else 0

            # Path B promoted: qualifying wallets WITH path_b_promoted tag
            row_pb = conn.execute("""
                SELECT COUNT(*) FROM discovered_entities
                WHERE insider_score >= ?
                  AND primary_tag = 'path_b_promoted'
            """, (INSIDER_SCORE_THRESHOLD,)).fetchone()
            self._path_b_promoted = row_pb[0] if row_pb else 0

            # Markets with >= MIN_CONSENSUS_SOURCES qualifying wallets in lookback window
            # D56: Run uncapped COUNT first to get true total, then capped query for detail
            row_total_count = conn.execute(f"""
                SELECT COUNT(*) FROM (
                    SELECT wo.market_id
                    FROM wallet_observations wo
                    INNER JOIN discovered_entities de ON de.entity_id = wo.address COLLATE NOCASE
                    WHERE wo.ingest_ts_utc > datetime('now', 'utc', ?)
                      AND de.insider_score >= ?
                    GROUP BY wo.market_id
                    HAVING COUNT(DISTINCT wo.address) >= ?
                )
            """, (lookback_str, INSIDER_SCORE_THRESHOLD, MIN_CONSENSUS_SOURCES)).fetchone()
            self._markets_consensus_total = row_total_count[0] if row_total_count else 0

            # LEFT JOIN polymarket_link_map to resolve human-readable slugs
            row_markets = conn.execute(f"""
                SELECT
                    plm.event_slug,
                    wo.market_id,
                    COUNT(DISTINCT wo.address) AS qualifying_wallet_count
                FROM wallet_observations wo
                INNER JOIN discovered_entities de ON de.entity_id = wo.address COLLATE NOCASE
                LEFT JOIN polymarket_link_map plm ON plm.token_id = wo.market_id COLLATE NOCASE
                WHERE wo.ingest_ts_utc > datetime('now', 'utc', ?)
                  AND de.insider_score >= ?
                GROUP BY wo.market_id
                HAVING qualifying_wallet_count >= ?
                ORDER BY MAX(wo.ingest_ts_utc) DESC
                LIMIT 10
            """, (lookback_str, INSIDER_SCORE_THRESHOLD, MIN_CONSENSUS_SOURCES)).fetchall()

            # r[0] = plm.event_slug (may be NULL), r[1] = raw market_id, r[2] = count
            self._markets_consensus_ready = len(row_markets)
            self._consensus_markets = [
                {
                    "slug": r[0] if r[0] else (
                        r[1][:16] + "..." if r[1] and len(r[1]) > 16 else (r[1] or "unknown")
                    ),
                    "wallet_count": r[2]
                }
                for r in row_markets
            ]
        except Exception as exc:
            logger.warning("[METRICS][CONSENSUS_SYNC] error: %s", exc)

    def sync_coverage_from_db(self, db) -> None:
        """[非決策路徑] 每 60s 同步 identity_coverage_log 統計。在 _metrics_json_loop 中調用。"""
        try:
            conn = db.conn if hasattr(db, "conn") else db
            for tier in ("t1", "t2", "t3", "t5"):
                row = conn.execute(
                    """
                    SELECT
                        COUNT(DISTINCT market_id)                           AS distinct_markets,
                        COUNT(*)                                            AS total_polls,
                        AVG(estimated_loss_rate)                            AS avg_loss_rate,
                        MAX(estimated_loss_rate)                            AS max_loss_rate,
                        AVG(wallet_coverage_rate)                           AS avg_wallet_coverage,
                        SUM(CASE WHEN api_page_saturated=1 THEN 1 ELSE 0 END) AS saturated_polls
                    FROM identity_coverage_log
                    WHERE market_tier = ? AND created_at > datetime('now', '-24 hours')
                    """,
                    (tier,),
                ).fetchone()
                if row and row[0]:
                    self.__dict__.setdefault("_coverage_stats", {})[tier] = {
                        "distinct_markets":   int(row[0]),
                        "total_polls":        int(row[1]),
                        "avg_loss_rate":      round(float(row[2] or 0), 4),
                        "max_loss_rate":      round(float(row[3] or 0), 4),
                        "avg_wallet_coverage": round(float(row[4] or 0), 4),
                        "saturated_polls":    int(row[5] or 0),
                    }
        except Exception as exc:
            logger.warning("[METRICS][COVERAGE_SYNC] error: %s", exc)

    def sync_te_stats(self) -> None:
        """[非決策路徑] 從 TE cache 讀取監控數值。不進入決策路徑。"""
        try:
            from panopticon_py.signal.transfer_entropy_cache import get_te_cache
            te = get_te_cache()
            self._te_stats = {
                "cached_te":   round(te.cached_value, 5),
                "significant":  te.is_significant,
                "skip_count":  te.skip_count,
            }
        except Exception:
            pass

    # ── Snapshot ─────────────────────────────────────────────────────────────

    def collect(self) -> MetricsSnapshot:
        """Compute a 1-second MetricsSnapshot."""
        now = time.time()
        elapsed_since_kyle = now - self._last_kyle_compute_ts if self._last_kyle_compute_ts > 0 else -1.0

        # Kyle P75 from last 5min of samples
        five_min_ago = now - 300
        recent = [(ts, aid, lam) for ts, aid, lam in self._kyle_samples if ts >= five_min_ago]
        distinct_assets = len({aid for _, aid, _ in recent})
        p75 = 0.0
        if recent:
            sorted_lambdas = sorted(lam for _, _, lam in recent)
            idx = int(len(sorted_lambdas) * 0.75)
            p75 = sorted_lambdas[min(idx, len(sorted_lambdas) - 1)]

        # Mean z-scores (from on_signal_queued calls from signal_engine)
        mean_z_t1 = float(sum(self._z_t1)) / len(self._z_t1) if self._z_t1 else 0.0
        mean_z_t2 = float(sum(self._z_t2)) / len(self._z_t2) if self._z_t2 else 0.0
        # D37 FIX: Also compute mean_z from entropy fires directly (_signal_z_vals)
        # This ensures mean_z shows non-zero even when on_signal_queued isn't called
        if self._signal_z_vals:
            cutoff = now - 300
            recent_z = [z for ts, z in self._signal_z_vals if ts >= cutoff]
            if recent_z:
                mean_z_t1 = float(sum(recent_z)) / len(recent_z)
        mean_p_t1 = float(sum(self._p_posterior_t1)) / len(self._p_posterior_t1) if self._p_posterior_t1 else 0.0
        mean_p_t2 = float(sum(self._p_posterior_t2)) / len(self._p_posterior_t2) if self._p_posterior_t2 else 0.0

        # ── Go-Live thresholds (Invariant 5.1) ─────────────────────────────────
        WIN_RATE_THRESHOLD = 0.55
        kyle_total = len(recent)
        paper_total = self._paper_trades_total
        paper_wins = self._paper_win_count
        win_rate = paper_wins / paper_total if paper_total > 0 else 0.0

        kyle_pct = min(1.0, kyle_total / 500.0)
        trades_pct = min(1.0, paper_total / 100.0)
        winrate_pct = win_rate / WIN_RATE_THRESHOLD if win_rate > 0 else 0.0
        winrate_pct = min(1.0, winrate_pct)

        locked = not (
            kyle_total >= 500
            and paper_total >= 100
            and win_rate >= WIN_RATE_THRESHOLD
        )

        return MetricsSnapshot(
            ts_utc=datetime.now(timezone.utc).isoformat(),
            ws=WsStats(
                connected=self._ws_connected,
                t1=self._t1, t2=self._t2, t3=self._t3, t5=self._t5,
                trade_ticks_60s=self._trade_ticks_60s.count(now),
                book_events_60s=self._book_events_60s.count(now),
                current_t1_window_start=self._current_t1_window_start,
                current_t1_window_end=self._current_t1_window_end,
                secs_remaining_in_window=self._secs_remaining,
                t1_rollover_count_today=self._t1_rollover_count,
                elapsed_since_last_ws_msg=now - self._last_ws_msg_ts if self._last_ws_msg_ts > 0 else 9999.0,
                # D121: WS subscription token tracking
                ws_subscribed_tokens=self._ws_subscribed_tokens,
                ws_total_tokens=self._ws_total_tokens,
                ws_last_payload_bytes=self._ws_last_payload_bytes,
            ),
            kyle=KyleStats(
                sample_count=len(recent),
                distinct_assets=distinct_assets,
                p75_estimate=p75,
                last_compute_elapsed_sec=elapsed_since_kyle,
                last_compute_status=self._last_kyle_status,
            ),
            window=WindowStats(
                active_entropy_windows=self._active_entropy_windows,
                last_cleanup_count=self._last_cleanup_count,
                last_cleanup_ts=self._last_cleanup_ts,
            ),
            queue=QueueStats(
                depth=self._queue_depth,
                processed_60s=self._processed_60s.count(now),
                mean_p_posterior_t1=mean_p_t1,
                mean_p_posterior_t2=mean_p_t2,
                mean_z_t1=mean_z_t1,
                mean_z_t2=mean_z_t2,
            ),
            gate=GateStats(
                evaluated_60s=self._gate_evaluated_rc.count(now),
                pass_count_60s=self._gate_pass_rc.count(now),
                abort_count_60s=self._gate_abort_rc.count(now),
                paper_trades_total=self._paper_trades_total,
                paper_win_count=self._paper_win_count,
                paper_win_rate=self._paper_win_rate,
                avg_ev=self._avg_ev,
            ),
            series=SeriesStats(
                deadline_ladders=self._deadline_ladders,
                rolling_windows=self._rolling_windows,
                total_series=self._total_series,
                monotone_violations=self._monotone_violations,
                last_violation_slug=self._last_violation_slug,
                last_violation_gap=self._last_violation_gap,
                catalyst_events_today=self._catalyst_events_today,
                oracle_high_risk=self._oracle_high_risk,
            ),
            consensus=ConsensusStats(
                qualifying_wallets=self._qualifying_wallets,
                new_candidates=self._new_candidates,
                path_b_promoted=self._path_b_promoted,
                markets_consensus_ready=self._markets_consensus_ready,
                markets_consensus_total=self._markets_consensus_total,
                consensus_markets=self._consensus_markets,
                price_debug={
                    "last_source": self._price_debug_last_source or "unknown",
                    "last_spread": self._price_debug_last_spread,
                    "no_price_count_24h": self._price_debug_no_price_rc.count(),
                },
            ),
            readiness=ReadinessSnapshot(
                kyle_pct=kyle_pct,
                trades_pct=trades_pct,
                winrate_pct=winrate_pct,
                all_ready=not locked,
            ),
            go_live=GoLiveSnapshot(
                locked=locked,
                kyle_pct=kyle_pct,
                trades_pct=trades_pct,
                winrate_pct=winrate_pct,
                kyle_total=kyle_total,
                paper_trades_total=paper_total,
                paper_win_count=paper_wins,
            ),
        )

    # ── Persist: split cadence ────────────────────────────────────────────────

    def persist_json(self, *, path: str = "data/rvf_live_snapshot.json") -> None:
        """
        Write snapshot to JSON file only (no DB write).
        Called every 5s from _metrics_json_loop() in run_radar.py.

        Uses atomic rename (write to .tmp then os.replace) to prevent
        FastAPI from reading a partially-written file.
        """
        import json as _json
        import os as _os
        snap = self.collect().to_dict()
        snap["_written_at"] = time.time()
        # D81: Inject synced coverage + TE stats (written by sync_coverage_from_db / sync_te_stats)
        snap["identity_coverage"] = getattr(self, "_coverage_stats", {})
        snap["transfer_entropy"]  = getattr(self, "_te_stats", {})
        tmp_path = path + ".tmp"
        try:
            _os.makedirs(_os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp_path, "w") as f:
                _json.dump(snap, f, separators=(",", ":"))
            _os.replace(tmp_path, path)  # atomic rename — no partial reads
        except Exception as exc:
            logger.warning("[METRICS_JSON][ERROR] %s", exc)

    def persist_db(self, db) -> None:
        """
        Write snapshot to rvf_metrics_snapshots DB table only (no JSON write).
        Called every 60s from radar heartbeat.
        """
        try:
            snap = self.collect().to_dict()
            db.write_rvf_snapshot(snap)
        except Exception as exc:
            logger.warning("[METRICS_DB][ERROR] %s", exc)

    def persist(self, db, *, path: str = "data/rvf_live_snapshot.json") -> None:
        """
        Legacy combined call — writes JSON + DB.
        Called by 60s radar heartbeat when the separate 5s JSON loop is not active.
        When _metrics_json_loop is running, heartbeat calls persist_db() only
        to avoid double JSON writes.
        """
        self.persist_json(path=path)
        self.persist_db(db)

    def latest_dict(self, *, path: str = "data/rvf_live_snapshot.json") -> dict:
        """Read last persisted snapshot from JSON file (called by FastAPI WS)."""
        import json
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    # D131: Debt-5 — getter for real_trade_ticks_60s upper-bound proxy
    def get_real_trade_ticks_60s(self) -> int:
        return self._real_trade_ticks_60s.count()

    def get_trade_ticks_60s(self) -> int:
        return self._trade_ticks_60s.count()
