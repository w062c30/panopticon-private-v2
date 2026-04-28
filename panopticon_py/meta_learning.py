from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from statistics import mean, pstdev


@dataclass(frozen=True)
class TradeRecord:
    pnl: float
    reason: str
    expected_ev: float
    posterior_probability: float
    alpha: float


@dataclass(frozen=True)
class BacktestSummary:
    sharpe: float
    max_drawdown: float
    net_profit: float
    recommended_alpha: float


def compute_max_drawdown(equity_curve: list[float]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            dd = (peak - x) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def compute_sharpe(pnls: list[float]) -> float:
    if len(pnls) < 2:
        return 0.0
    mu = mean(pnls)
    sigma = pstdev(pnls)
    if sigma == 0:
        return 0.0
    return (mu / sigma) * sqrt(len(pnls))


def optimize_alpha(trades: list[TradeRecord], candidates: list[float]) -> float:
    if not trades:
        return candidates[0]
    best_alpha = candidates[0]
    best_score = float("-inf")
    base = [t.pnl for t in trades]
    for alpha in candidates:
        scaled = [p * (alpha / max(trades[0].alpha, 1e-6)) for p in base]
        score = compute_sharpe(scaled)
        if score > best_score:
            best_score = score
            best_alpha = alpha
    return best_alpha


def summarize(trades: list[TradeRecord], initial_capital: float = 2000.0) -> BacktestSummary:
    pnls = [t.pnl for t in trades]
    equity = [initial_capital]
    for p in pnls:
        equity.append(equity[-1] + p)
    best_alpha = optimize_alpha(trades, [0.125, 0.25, 0.375, 0.5])
    return BacktestSummary(
        sharpe=compute_sharpe(pnls),
        max_drawdown=compute_max_drawdown(equity),
        net_profit=sum(pnls),
        recommended_alpha=best_alpha,
    )
