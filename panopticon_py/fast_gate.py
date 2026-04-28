from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from math import exp

from panopticon_py.execution.constants import (
    REASON_DEGRADED_AND_EV_NOT_ENOUGH,
    REASON_L2_TIMEOUT_DEGRADED,
    REASON_NETWORK_LATENCY_BREAKER,
    REASON_NON_POSITIVE_TIME_ADJUSTED_EV,
    REASON_PASS,
    REASON_SLIPPAGE_TOLERANCE_EXCEEDED,
)
from panopticon_py.friction_state import FrictionSnapshot


class GateDecision(str, Enum):
    ABORT = "ABORT"
    DEGRADE = "DEGRADE"
    EXECUTE = "EXECUTE"


@dataclass(frozen=True)
class FastSignalInput:
    p_prior: float
    quote_price: float
    payout: float
    capital_in: float
    order_size: float
    delta_t_ms: float
    gamma: float
    slippage_tolerance: float
    min_ev_threshold: float
    daily_opp_cost: float
    days_to_resolution: float
    bid_ask_imbalance: float = 0.0
    avg_entry_price: float = 0.0


@dataclass(frozen=True)
class FastGateOutput:
    decision: GateDecision
    p_adjusted: float
    p_avg: float
    ev_net: float
    ev_time_adj: float
    expected_slippage: float
    kelly_cap: float
    reason: str


def _bounded_prob(p: float) -> float:
    return min(max(p, 1e-6), 1 - 1e-6)


def fast_execution_gate(signal: FastSignalInput, snapshot: FrictionSnapshot) -> FastGateOutput:
    ping_breaker_ms = float(os.getenv("FAST_GATE_PING_BREAK_MS", "200"))
    bai_multiplier = float(os.getenv("FAST_GATE_BAI_MULTIPLIER", "3.0"))
    slip_tolerance = float(os.getenv("FAST_GATE_SLIPPAGE_TOLERANCE", "0.01"))
    degraded_kelly = float(os.getenv("FAST_GATE_DEGRADED_KELLY_CAP", "0.1"))

    if snapshot.network_ping_ms > ping_breaker_ms:
        return FastGateOutput(
            decision=GateDecision.ABORT,
            p_adjusted=signal.p_prior,
            p_avg=signal.quote_price,
            ev_net=-1.0,
            ev_time_adj=-1.0,
            expected_slippage=0.0,
            kelly_cap=0.0,
            reason=REASON_NETWORK_LATENCY_BREAKER,
        )

    p_adjusted = _bounded_prob(_bounded_prob(signal.p_prior) * exp(-signal.gamma * max(0.0, signal.delta_t_ms)))
    bai = max(0.0, min(1.0, signal.bid_ask_imbalance))
    expected_slippage = signal.order_size * snapshot.kyle_lambda * (1.0 + bai_multiplier * bai)
    p_avg = signal.quote_price + expected_slippage
    taker_fee = snapshot.current_base_fee * p_avg * (1 - p_avg)
    slippage_cost = max(0.0, p_avg - signal.quote_price)
    ev_net = (
        (p_adjusted * signal.payout * signal.order_size)
        - signal.capital_in
        - (signal.avg_entry_price * signal.order_size)
        - taker_fee
        - snapshot.gas_cost_estimate
        - slippage_cost
    )
    ev_time_adj = ev_net - (signal.capital_in * signal.daily_opp_cost * signal.days_to_resolution)

    if snapshot.degraded:
        if ev_time_adj <= signal.min_ev_threshold:
            return FastGateOutput(
                decision=GateDecision.ABORT,
                p_adjusted=p_adjusted,
                p_avg=p_avg,
                ev_net=ev_net,
                ev_time_adj=ev_time_adj,
                expected_slippage=expected_slippage,
                kelly_cap=degraded_kelly,
                reason=REASON_DEGRADED_AND_EV_NOT_ENOUGH,
            )
        return FastGateOutput(
            decision=GateDecision.DEGRADE,
            p_adjusted=p_adjusted,
            p_avg=p_avg,
            ev_net=ev_net,
            ev_time_adj=ev_time_adj,
            expected_slippage=expected_slippage,
            kelly_cap=degraded_kelly,
            reason=REASON_L2_TIMEOUT_DEGRADED,
        )

    if signal.slippage_tolerance < slip_tolerance and expected_slippage > signal.slippage_tolerance:
        return FastGateOutput(
            decision=GateDecision.ABORT,
            p_adjusted=p_adjusted,
            p_avg=p_avg,
            ev_net=ev_net,
            ev_time_adj=ev_time_adj,
            expected_slippage=expected_slippage,
            kelly_cap=snapshot.kelly_cap,
            reason=REASON_SLIPPAGE_TOLERANCE_EXCEEDED,
        )

    if ev_time_adj <= signal.min_ev_threshold:
        return FastGateOutput(
            decision=GateDecision.ABORT,
            p_adjusted=p_adjusted,
            p_avg=p_avg,
            ev_net=ev_net,
            ev_time_adj=ev_time_adj,
            expected_slippage=expected_slippage,
            kelly_cap=snapshot.kelly_cap,
            reason=REASON_NON_POSITIVE_TIME_ADJUSTED_EV,
        )

    return FastGateOutput(
        decision=GateDecision.EXECUTE,
        p_adjusted=p_adjusted,
        p_avg=p_avg,
        ev_net=ev_net,
        ev_time_adj=ev_time_adj,
        expected_slippage=expected_slippage,
        kelly_cap=snapshot.kelly_cap,
        reason=REASON_PASS,
    )

