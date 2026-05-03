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
