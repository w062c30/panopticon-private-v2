"""Original strategy decision core (moved from panopticon_py.strategy to avoid package shadowing)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StrategyInput:
    prior_probability: float
    likelihood_ratio: float
    price: float
    fee_rate: float
    slippage_pct: float
    alpha: float
    ask_entry_price: Optional[float] = None
    bid_exit_price: Optional[float] = None
    allow_trade: bool = True


@dataclass(frozen=True)
class StrategyOutput:
    posterior_probability: float
    ev_net: float
    kelly_fraction: float
    action: str
    price_used: float = 0.0


def bayesian_update(prior_probability: float, likelihood_ratio: float) -> float:
    prior_probability = min(max(prior_probability, 1e-6), 1 - 1e-6)
    odds_prior = prior_probability / (1 - prior_probability)
    odds_post = odds_prior * likelihood_ratio
    return odds_post / (1 + odds_post)


def fractional_kelly(p: float, price: float, alpha: float) -> float:
    p = min(max(p, 0.0), 1.0)
    b = (1 - price) / max(price, 1e-6)
    q = 1 - p
    raw = (b * p - q) / max(b, 1e-6)
    return max(0.0, alpha * raw)


def ev_net(
    p: float,
    price: float,
    fee_rate: float,
    slippage_pct: float,
    *,
    ask_entry_price: float | None = None,
    bid_exit_price: float | None = None,
) -> float:
    entry = ask_entry_price if ask_entry_price is not None else price
    exitp = bid_exit_price if bid_exit_price is not None else price
    gross = p * (1 - entry) - (1 - p) * entry
    micro_cost = 0.0
    if ask_entry_price is not None and bid_exit_price is not None:
        micro_cost = max(0.0, ask_entry_price - bid_exit_price)
    costs = fee_rate + slippage_pct + micro_cost
    return gross - costs


def decide(si: StrategyInput) -> StrategyOutput:
    if si.slippage_pct > 0.02:
        return StrategyOutput(
            posterior_probability=si.prior_probability,
            ev_net=-1.0,
            kelly_fraction=0.0,
            action="HOLD",
            price_used=si.price,
        )
    posterior = bayesian_update(si.prior_probability, si.likelihood_ratio)
    entry_for_kelly = si.ask_entry_price if si.ask_entry_price is not None else si.price
    expected = ev_net(
        posterior,
        si.price,
        si.fee_rate,
        si.slippage_pct,
        ask_entry_price=si.ask_entry_price,
        bid_exit_price=si.bid_exit_price,
    )
    kelly = fractional_kelly(posterior, entry_for_kelly, si.alpha)
    action = "BUY" if si.allow_trade and expected > 0 and kelly > 0 else "HOLD"
    return StrategyOutput(
        posterior_probability=posterior,
        ev_net=expected,
        kelly_fraction=kelly,
        action=action,
        price_used=entry_for_kelly,
    )
