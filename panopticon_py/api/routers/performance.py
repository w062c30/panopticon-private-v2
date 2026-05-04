from __future__ import annotations

import os
from fastapi import APIRouter, Query, Request

from panopticon_py.api.schemas import (
    MaxDrawdownInfo,
    PerformanceHistoryPoint,
    PerformanceHistoryResponse,
    PerformanceResponse,
    PolMarketEntry,
    T5CoverageResponse,
    TierMarketEntry,
    WatchlistResponse,
)
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.live_trade_pnl_service import compute_live_history, compute_live_performance, fetch_live_trade_rows

router = APIRouter(prefix="/api", tags=["performance"])

# D103-FE: Module-level constant, read once at process start
_DEBUG_STATS_ENABLED = os.getenv("DEBUG_STATS_ENABLED", "false").lower() == "true"
# D164: extend GET /api/entropy/status when backend started with LOG_LEVEL=DEBUG
_LOG_LEVEL_DEBUG = os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG"


@router.get("/performance", response_model=PerformanceResponse)
def get_performance(period: str = Query("all", pattern="^(1d|7d|30d|all)$")) -> PerformanceResponse:
    db = ShadowDB()
    try:
        db.bootstrap()
        live_rows = fetch_live_trade_rows(limit=300)
        metrics = compute_live_performance(live_rows, period=period) if live_rows else db.fetch_performance_metrics(period=period)
    finally:
        db.close()
    return PerformanceResponse(
        period=period,
        totalPnlUsd=metrics["total_pnl_usd"],
        winRate=metrics["win_rate"],
        sharpeRatio=metrics["sharpe_ratio"],
        maxDrawdown=MaxDrawdownInfo(
            value=metrics["max_drawdown"],
            peakTs=metrics["peak_ts"],
            troughTs=metrics["trough_ts"],
            fromTradeId=metrics["from_trade_id"],
            toTradeId=metrics["to_trade_id"],
        ),
        profitFactor=metrics["profit_factor"],
        slippageGap=metrics["slippage_gap"],
        tradeCount=metrics["trade_count"],
    )


@router.get("/performance/history", response_model=PerformanceHistoryResponse)
def get_performance_history(period: str = Query("all", pattern="^(1d|7d|30d|all)$")) -> PerformanceHistoryResponse:
    db = ShadowDB()
    try:
        db.bootstrap()
        live_rows = fetch_live_trade_rows(limit=300)
        points = compute_live_history(live_rows, period=period) if live_rows else db.fetch_performance_history(period=period)
    finally:
        db.close()

    return PerformanceHistoryResponse(
        period=period,
        points=[
            PerformanceHistoryPoint(
                ts=str(p["ts"]),
                cumulativePnlUsd=float(p["cumulative_pnl_usd"]),
            )
            for p in points
        ],
    )


@router.get("/t5-coverage", response_model=T5CoverageResponse)
def get_t5_coverage() -> T5CoverageResponse:
    """
    D102: T5 Sports Market Coverage Panel.
    Returns 24h signal/execution/pass-rate summary for T5 tier.
    """
    db = ShadowDB()
    try:
        db.bootstrap()
        return T5CoverageResponse(**db.fetch_t5_coverage_summary())
    finally:
        db.close()


import json as _json
from pathlib import Path as _Path

def _entropy_status_debug_detail_enabled() -> bool:
    return _DEBUG_STATS_ENABLED or _LOG_LEVEL_DEBUG


@router.get("/entropy/status")
def get_entropy_status() -> dict:
    """
    D157-2: Read entropy snapshot written by **radar** every 5s (``data/entropy_status.json``).
    File-based IPC — backend does not import radar's EntropyWindow dict.

    D164: When ``DEBUG_STATS_ENABLED=true`` or ``LOG_LEVEL=DEBUG`` at backend startup,
    merge ``rvf_latest`` (last ``rvf_metrics_snapshots`` row) and effective entropy env knobs.
    """
    snap_path = _Path(os.getenv("ENTROPY_STATUS_PATH", "data/entropy_status.json"))
    try:
        if not snap_path.exists():
            base: dict = {"error": "not_ready", "tokens": {}, "total": 0, "z_ready_count": 0}
        else:
            raw = _json.loads(snap_path.read_text(encoding="utf-8"))
            base = raw if isinstance(raw, dict) else {"error": "invalid_json", "detail": type(raw).__name__}
    except Exception as exc:
        base = {"error": str(exc), "tokens": {}, "total": 0, "z_ready_count": 0}

    if not _entropy_status_debug_detail_enabled():
        return base

    from config import get_min_history_for_z, get_z_threshold

    extra: dict = {
        "d165_debug": True,  # D164: was d164_debug; D165: unified debug version tag
        "min_history_for_z_effective": get_min_history_for_z(),
        "min_entropy_z_threshold_effective": get_z_threshold(),
        "hunt_min_history_env": os.getenv("HUNT_MIN_HISTORY_FOR_Z"),
        "hunt_min_entropy_z_env": os.getenv("HUNT_MIN_ENTROPY_Z_THRESHOLD"),
        "hunt_ew_unlock_event_count_env": os.getenv("HUNT_EW_UNLOCK_EVENT_COUNT"),
        "hunt_ew_unlock_healthy_span_env": os.getenv("HUNT_EW_UNLOCK_HEALTHY_SPAN_SEC"),
    }
    db: ShadowDB | None = None
    try:
        db = ShadowDB()
        db.bootstrap()
        row = db.conn.execute(
            """
            SELECT ts_utc,
                   active_ew AS active_entropy_windows,
                   mean_z_t1, mean_z_t2,
                   gate_eval_60s, gate_pass_60s, gate_abort_60s
            FROM rvf_metrics_snapshots
            ORDER BY ts_utc DESC
            LIMIT 1
            """
        ).fetchone()
        if row:
            rd = dict(row)
            ge = int(rd.get("gate_eval_60s") or 0)
            gp = int(rd.get("gate_pass_60s") or 0)
            if gp > 0:
                zst = "OK"
            elif ge <= 0:
                zst = "NO_EVAL"
            else:
                zst = "BLOCKED"
            rd["z_ready_status"] = zst
            extra["rvf_latest"] = rd
        else:
            extra["rvf_latest"] = None
    except Exception as exc:
        extra["rvf_latest_error"] = str(exc)
    finally:
        if db is not None:
            db.close()

    if isinstance(base, dict):
        return {**base, **extra}
    return {**extra, "snapshot_body": base}


@router.get("/async-writer-health")
def get_async_writer_health(request: Request) -> dict:
    """
    D118: AsyncDBWriter queue health — running, thread_alive, queue_depth, queue_unfinished.
    Backend returns stub (running=False) since the real writer runs in the orchestrator process.
    """
    writer = getattr(request.app.state, "async_writer", None)
    if writer is None:
        return {"error": "async_writer not initialized"}
    return writer.health()


@router.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist() -> WatchlistResponse:
    """
    D103-FE: Market execution watchlist — 48h window.

    IMPORTANT: This endpoint reads ``execution_records`` table only.
    It shows markets with signals/trades in the past 48h, NOT the
    current radar subscription list.

    For radar's active subscription list, use:
        GET /api/radar/active-markets   <- reads radar_active_markets.json
        GET /api/watchlist              <- reads execution_records (this endpoint)

    NOTE: Relies on lifespan bootstrap() for table initialization (see app.py:48).
    """
    db = ShadowDB()
    # D104: watchlist is read-only; bootstrap() is called once at process startup in lifespan
    try:
        pol = db.fetch_active_pol_markets()
        t1  = db.fetch_active_markets_by_tier("t1", lookback_hours=48)
        t2  = db.fetch_active_markets_by_tier("t2", lookback_hours=48)
        t3  = db.fetch_active_markets_by_tier("t3", lookback_hours=48)
        t4  = db.fetch_active_markets_by_tier("t4", lookback_hours=48)
        t5  = db.fetch_active_markets_by_tier("t5", lookback_hours=48)
        return WatchlistResponse(
            pol_markets=[PolMarketEntry(**m) for m in pol],
            t1_markets=[TierMarketEntry(**m) for m in t1],
            t2_markets=[TierMarketEntry(**m) for m in t2],
            t3_markets=[TierMarketEntry(**m) for m in t3],
            t4_markets=[TierMarketEntry(**m) for m in t4],
            t5_markets=[TierMarketEntry(**m) for m in t5],
            tier_available={
                "t1":     len(t1)  > 0,
                "t2":     len(t2)  > 0,
                "t2_pol": len(pol) > 0,
                "t3":     len(t3)  > 0,
                "t4":     len(t4)  > 0,
                "t5":     len(t5)  > 0,
            },
        )
    finally:
        db.close()


@router.get("/watchlist/market-debug-stats")
def get_market_debug_stats():
    """
    D103-FE DEBUG: Per-market deep stats.
    Returns {"enabled": false, "markets": {}} in production.
    Activate via: DEBUG_STATS_ENABLED=true (env var at process start).
    """
    if not _DEBUG_STATS_ENABLED:
        return {"enabled": False, "markets": {}}
    db = ShadowDB()
    try:
        db.bootstrap()
        stats = db.fetch_market_debug_stats()
        return {"enabled": True, "markets": stats}
    finally:
        db.close()
