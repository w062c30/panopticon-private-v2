from __future__ import annotations

from fastapi import APIRouter, Query, Request

from panopticon_py.api.schemas import (
    MaxDrawdownInfo,
    PerformanceHistoryPoint,
    PerformanceHistoryResponse,
    PerformanceResponse,
    PolMarketEntry,
    T5CoverageResponse,
    T5MarketEntry,
    WatchlistResponse,
)
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.live_trade_pnl_service import compute_live_history, compute_live_performance, fetch_live_trade_rows

router = APIRouter(prefix="/api", tags=["performance"])


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
    D103: Combined political + T5 sports market watchlist.
    Returns which markets are currently being monitored, along with
    availability flags so the frontend can display "no data" states cleanly.
    """
    db = ShadowDB()
    try:
        db.bootstrap()
        pol = db.fetch_active_pol_markets()
        t5 = db.fetch_active_t5_markets(lookback_hours=48)
        return WatchlistResponse(
            pol_markets=[PolMarketEntry(**m) for m in pol],
            t5_markets=[T5MarketEntry(**m) for m in t5],
            pol_count=len(pol),
            t5_count=len(t5),
            pol_data_available=len(pol) > 0,
            t5_data_available=len(t5) > 0,
        )
    finally:
        db.close()
