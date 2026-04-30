from __future__ import annotations

from pydantic import BaseModel


class TradeItem(BaseModel):
    tradeId: str
    marketId: str
    eventName: str
    eventUrl: str | None = None
    linkType: str | None = None
    linkSource: str | None = None
    linkReason: str | None = None
    direction: str
    confidence: float | None = None
    openReason: str
    entryPrice: float | None = None
    exitPrice: float | None = None
    positionSizeUsd: float | None = None
    estimatedEvUsd: float | None = None
    realizedPnlUsd: float | None = None
    unrealizedPnlUsd: float = 0.0
    status: str
    markPrice: float | None = None
    updatedAt: str
    closeCondition: str
    openedAt: str
    closedAt: str
    source: str = "live"


class RecommendationsResponse(BaseModel):
    trades: list[TradeItem]


class MaxDrawdownInfo(BaseModel):
    value: float
    peakTs: str | None = None
    troughTs: str | None = None
    fromTradeId: str | None = None
    toTradeId: str | None = None


class PerformanceResponse(BaseModel):
    period: str
    totalPnlUsd: float
    winRate: float | None = None
    sharpeRatio: float
    maxDrawdown: MaxDrawdownInfo
    profitFactor: float | None = None
    slippageGap: float | None = None
    tradeCount: int


class PerformanceHistoryPoint(BaseModel):
    ts: str
    cumulativePnlUsd: float


class PerformanceHistoryResponse(BaseModel):
    period: str
    points: list[PerformanceHistoryPoint]


class ReadinessResponse(BaseModel):
    currentPaperTrades: int
    targetTrades: int
    runningDays: int
    targetDays: int
    currentWinRate: float | None = None
    isReady: bool


class SystemStatusResponse(BaseModel):
    state: str
    message: str
    lastEventTs: str | None = None
    lastDecisionId: str | None = None
    lastExecutionReason: str | None = None
    lastRejectReason: str | None = None


class ReportCounts(BaseModel):
    openTrades: int
    closedTrades: int
    uniqueMarkets: int
    canonicalHitRate: float


class ReportPnl(BaseModel):
    realizedTotalUsd: float
    unrealizedTotalUsd: float
    netTotalUsd: float


class ReportQuality(BaseModel):
    fallbackRate: float
    unresolvedCount: int


class ReportCurrentResponse(BaseModel):
    counts: ReportCounts
    pnl: ReportPnl
    quality: ReportQuality
    findings: list[str]
    updatedAt: str


class T5CoverageResponse(BaseModel):
    """D102: Pydantic model for /api/t5-coverage response."""
    tier: str
    period: str
    total_signals: int
    accepted: int
    rejected: int
    avg_ev_accepted: float | None = None
    avg_posterior: float | None = None
    distinct_markets: int
    pass_rate: float | None = None


class PolMarketEntry(BaseModel):
    """D103: Pydantic model for a single political market in the watchlist."""
    market_id: str
    event_slug: str | None
    political_category: str
    entity_keywords: list[str]
    token_id: str | None
    token_id_no: str | None
    subscribed_at: str
    last_signal_ts: str | None


class T5MarketEntry(BaseModel):
    """D103: Pydantic model for a single T5 sports market in the watchlist."""
    market_id: str
    total_signals: int
    accepted: int
    last_signal_ts: str | None
    avg_ev: float | None


class TierMarketEntry(BaseModel):
    """D103-FE: Pydantic model for a single T1/T2/T3/T4/T5 market in the watchlist."""
    market_id: str
    market_tier: str
    total_signals: int
    accepted: int
    last_signal_ts: str | None
    avg_ev: float | None
    avg_posterior: float | None


class WatchlistResponse(BaseModel):
    """D103-FE: Combined political + T5 watchlist with availability flags."""
    pol_markets: list[PolMarketEntry]
    t1_markets: list[TierMarketEntry]
    t2_markets: list[TierMarketEntry]
    t3_markets: list[TierMarketEntry]
    t4_markets: list[TierMarketEntry]
    t5_markets: list[TierMarketEntry]
    tier_available: dict[str, bool]
