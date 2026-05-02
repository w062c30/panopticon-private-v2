"""Panopticon Metrics — Snapshot dataclass."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WsStats:
    """WebSocket subscription statistics."""
    connected: bool = False
    t1: int = 0
    t2: int = 0
    t3: int = 0
    t5: int = 0
    trade_ticks_60s: int = 0
    book_events_60s: int = 0
    current_t1_window_start: int = 0
    current_t1_window_end: int = 0
    secs_remaining_in_window: float = 0.0
    t1_rollover_count_today: int = 0
    elapsed_since_last_ws_msg: float = 0.0
    # D121: WS subscription token tracking
    ws_subscribed_tokens: int = 0  # tokens actually sent to WS (ws_only subset)
    ws_total_tokens: int = 0  # total tokens in all tiers (includes REST-polled T3/T5)
    ws_last_payload_bytes: int = 0  # bytes of last WS subscription payload


@dataclass
class KyleStats:
    """Kyle lambda accumulation statistics."""
    sample_count: int = 0
    distinct_assets: int = 0
    p75_estimate: float = 0.0
    last_compute_elapsed_sec: float = -1.0
    last_compute_status: str = "none"  # "none" | "skip" | "compute"


@dataclass
class WindowStats:
    """T1 EntropyWindow statistics."""
    active_entropy_windows: int = 0
    last_cleanup_count: int = 0
    last_cleanup_ts: float = 0.0


@dataclass
class QueueStats:
    """Signal queue depth statistics."""
    depth: int = 0
    processed_60s: int = 0
    mean_p_posterior_t1: float = 0.0
    mean_p_posterior_t2: float = 0.0
    mean_z_t1: float = 0.0
    mean_z_t2: float = 0.0


@dataclass
class GateStats:
    """EV gate evaluation statistics."""
    evaluated_60s: int = 0
    pass_count_60s: int = 0
    abort_count_60s: int = 0
    paper_trades_total: int = 0
    paper_win_count: int = 0
    paper_win_rate: float = 0.0
    avg_ev: float = 0.0


@dataclass
class SeriesStats:
    """Event series intelligence statistics."""
    deadline_ladders: int = 0
    rolling_windows: int = 0
    total_series: int = 0
    monotone_violations: int = 0
    last_violation_slug: str = ""
    last_violation_gap: float = 0.0
    catalyst_events_today: int = 0
    oracle_high_risk: int = 0


@dataclass
class ReadinessSnapshot:
    """Go-live readiness summary."""
    kyle_pct: float = 0.0  # 0.0–1.0
    trades_pct: float = 0.0
    winrate_pct: float = 0.0
    all_ready: bool = False


@dataclass
class GoLiveSnapshot:
    """Go-live lock/unlock state — authoritative threshold gate."""
    locked: bool = True   # True = at least one threshold not met
    kyle_pct: float = 0.0
    trades_pct: float = 0.0
    winrate_pct: float = 0.0
    kyle_total: int = 0
    paper_trades_total: int = 0
    paper_win_count: int = 0


@dataclass
class ConsensusStats:
    """Insider consensus / wallet readiness statistics."""
    qualifying_wallets: int = 0           # total distinct wallets with insider_score >= threshold
    new_candidates: int = 0                # wallets without path_b_promoted tag
    path_b_promoted: int = 0               # wallets promoted via Path B
    markets_consensus_ready: int = 0      # markets meeting MIN_CONSENSUS_SOURCES (capped at 10)
    markets_consensus_total: int = 0      # true count without LIMIT — for display only
    consensus_markets: list = field(default_factory=list)  # [{slug, wallet_count}] latest 10
    price_debug: dict = field(default_factory=dict)       # {last_source, last_spread, no_price_count_24h}

    def to_dict(self) -> dict[str, Any]:
        d = {
            "qualifying_wallets": self.qualifying_wallets,
            "new_candidates": self.new_candidates,
            "path_b_promoted": self.path_b_promoted,
            "markets_consensus_ready": self.markets_consensus_ready,
            "markets_consensus_total": self.markets_consensus_total,
            "consensus_markets": self.consensus_markets,
        }
        if self.price_debug:
            d["price_debug"] = self.price_debug
        return d


@dataclass
class MetricsSnapshot:
    """
    Panopticon RVF Metrics Snapshot — pushed to frontend every 1s.

    Collected by MetricsCollector (in-process, no DB reads in hot path).
    Written to rvf_metrics_snapshots DB table every 5s for persistence.
    """
    ts_utc: str = ""
    ws: WsStats = field(default_factory=WsStats)
    kyle: KyleStats = field(default_factory=KyleStats)
    window: WindowStats = field(default_factory=WindowStats)
    queue: QueueStats = field(default_factory=QueueStats)
    gate: GateStats = field(default_factory=GateStats)
    series: SeriesStats = field(default_factory=SeriesStats)
    consensus: ConsensusStats = field(default_factory=ConsensusStats)
    readiness: ReadinessSnapshot = field(default_factory=ReadinessSnapshot)
    go_live: GoLiveSnapshot = field(default_factory=GoLiveSnapshot)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "ws": {"connected": self.ws.connected, "t1": self.ws.t1, "t2": self.ws.t2,
                   "t3": self.ws.t3, "t5": self.ws.t5, "trade_ticks_60s": self.ws.trade_ticks_60s,
                   "book_events_60s": self.ws.book_events_60s,
                   "current_t1_window_start": self.ws.current_t1_window_start,
                   "current_t1_window_end": self.ws.current_t1_window_end,
                   "secs_remaining_in_window": self.ws.secs_remaining_in_window,
                   "t1_rollover_count_today": self.ws.t1_rollover_count_today,
                   "elapsed_since_last_ws_msg": self.ws.elapsed_since_last_ws_msg},
            "kyle": {"sample_count": self.kyle.sample_count, "distinct_assets": self.kyle.distinct_assets,
                     "p75_estimate": self.kyle.p75_estimate,
                     "last_compute_elapsed_sec": self.kyle.last_compute_elapsed_sec,
                     "last_compute_status": self.kyle.last_compute_status},
            "window": {"active_entropy_windows": self.window.active_entropy_windows,
                      "last_cleanup_count": self.window.last_cleanup_count,
                      "last_cleanup_ts": self.window.last_cleanup_ts},
            "queue": {"depth": self.queue.depth, "processed_60s": self.queue.processed_60s,
                      "mean_p_posterior_t1": self.queue.mean_p_posterior_t1,
                      "mean_p_posterior_t2": self.queue.mean_p_posterior_t2,
                      "mean_z_t1": self.queue.mean_z_t1, "mean_z_t2": self.queue.mean_z_t2},
            "gate": {"evaluated_60s": self.gate.evaluated_60s, "pass_count_60s": self.gate.pass_count_60s,
                     "abort_count_60s": self.gate.abort_count_60s,
                     "paper_trades_total": self.gate.paper_trades_total,
                     "paper_win_count": self.gate.paper_win_count,
                     "paper_win_rate": self.gate.paper_win_rate, "avg_ev": self.gate.avg_ev},
            "series": {"deadline_ladders": self.series.deadline_ladders,
                       "rolling_windows": self.series.rolling_windows,
                       "total_series": self.series.total_series,
                       "monotone_violations": self.series.monotone_violations,
                       "last_violation_slug": self.series.last_violation_slug,
                       "last_violation_gap": self.series.last_violation_gap,
                       "catalyst_events_today": self.series.catalyst_events_today,
                       "oracle_high_risk": self.series.oracle_high_risk},
            "consensus": self.consensus.to_dict(),
            "readiness": {"kyle_pct": self.readiness.kyle_pct, "trades_pct": self.readiness.trades_pct,
                          "winrate_pct": self.readiness.winrate_pct, "all_ready": self.readiness.all_ready},
            "go_live": {"locked": self.go_live.locked,
                        "kyle_pct": self.go_live.kyle_pct,
                        "trades_pct": self.go_live.trades_pct,
                        "winrate_pct": self.go_live.winrate_pct,
                        "kyle_total": self.go_live.kyle_total,
                        "paper_trades_total": self.go_live.paper_trades_total,
                        "paper_win_count": self.go_live.paper_win_count},
            # D81: Identity coverage + Transfer Entropy — injected by MetricsCollector
            # (populated by sync_coverage_from_db() + sync_te_stats() at 60s cadence)
            "identity_coverage": {},
            "transfer_entropy": {},
        }
