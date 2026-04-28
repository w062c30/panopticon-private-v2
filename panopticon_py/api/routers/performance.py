from __future__ import annotations

from fastapi import APIRouter, Query

from panopticon_py.api.schemas import MaxDrawdownInfo, PerformanceHistoryPoint, PerformanceHistoryResponse, PerformanceResponse
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
