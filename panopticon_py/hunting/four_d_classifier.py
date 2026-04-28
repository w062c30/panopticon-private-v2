"""4D-style entity classifier: IDI, burstiness, taker ratio — MM vs insider slicing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from panopticon_py.hunting.trade_aggregate import ParentTrade, VirtualEntity

EntityLabel = Literal[
    "POTENTIAL_INSIDER",
    "INSIDER_ALGO_SLICING",
    "MARKET_MAKER_NOISE",
    "UNCERTAIN_NOISE",
    "COORDINATED_SMURF",
]


@dataclass(frozen=True)
class FourDScores:
    idi: float
    burst: float
    taker_ratio: float


def _gini(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    cum = 0.0
    for i, v in enumerate(s, start=1):
        cum += i * v
    denom = n * sum(s)
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, (2 * cum / denom) - (n + 1) / n))


def scores_from_parents(parents: list[ParentTrade], *, assume_all_taker: bool = True) -> FourDScores:
    """IDI from signed volume proxy (side * volume), burst from inter-arrival gini, taker from flags."""
    if not parents:
        return FourDScores(0.0, 0.0, 0.0)
    net = sum(p.side * p.volume for p in parents)
    tot = sum(p.volume for p in parents) or 1.0
    idi = abs(net) / tot
    gaps: list[float] = []
    ordered = sorted(parents, key=lambda p: p.first_ts_ms)
    for a, b in zip(ordered, ordered[1:]):
        g = max(0.0, b.first_ts_ms - a.last_ts_ms)
        gaps.append(g)
    burst = _gini(gaps) if gaps else 0.0
    taker_ratio = 1.0 if assume_all_taker else 0.8
    return FourDScores(idi=idi, burst=burst, taker_ratio=taker_ratio)


def classify_high_frequency_wallet(
    parents: list[ParentTrade],
    *,
    low_freq_threshold: int | None = None,
    assume_all_taker: bool = False,
) -> tuple[EntityLabel, FourDScores, list[str]]:
    """
    Decision tree per hunting plan. ``parents`` should already be sweep-aggregated.
    """
    thr = int(low_freq_threshold if low_freq_threshold is not None else os.getenv("HUNT_LOW_FREQ_PARENT_THRESHOLD", "8"))
    idi_hi = float(os.getenv("HUNT_IDI_HIGH", "0.8"))
    idi_lo = float(os.getenv("HUNT_IDI_LOW", "0.3"))
    tk_hi = float(os.getenv("HUNT_TAKER_HIGH", "0.7"))
    tk_lo = float(os.getenv("HUNT_TAKER_LOW", "0.2"))
    bu_hi = float(os.getenv("HUNT_BURST_HIGH", "0.8"))

    reasons: list[str] = []
    s = scores_from_parents(parents, assume_all_taker=assume_all_taker)
    if len(parents) < thr:
        reasons.append("low_parent_count")
        return "POTENTIAL_INSIDER", s, reasons

    if s.idi > idi_hi and s.taker_ratio > tk_hi and s.burst > bu_hi:
        reasons.append("idi_high_taker_high_burst_high")
        return "INSIDER_ALGO_SLICING", s, reasons
    if s.idi < idi_lo and s.taker_ratio < tk_lo:
        reasons.append("inventory_neutral_low_taker")
        return "MARKET_MAKER_NOISE", s, reasons
    reasons.append("ambiguous_middle_region")
    return "UNCERTAIN_NOISE", s, reasons


def classify_virtual_entity(ve: VirtualEntity) -> tuple[EntityLabel, FourDScores, list[str]]:
    """Cross-wallet burst: primary label COORDINATED_SMURF when cluster is substantial."""
    agg = [
        ParentTrade(
            taker=ve.entity_id,
            side=ve.side,
            volume=ve.total_volume,
            first_ts_ms=ve.first_ts_ms,
            last_ts_ms=ve.last_ts_ms,
            child_count=ve.trade_count,
            market_id=None,
        )
    ]
    scores = scores_from_parents(agg, assume_all_taker=os.getenv("HUNT_ASSUME_ALL_TAKER", "0") == "1")
    if ve.trade_count >= 3 and len(ve.members) >= 3 and ve.total_volume > 0:
        return "COORDINATED_SMURF", scores, ["cross_wallet_cluster", f"members={len(ve.members)}"]
    assume_taker = os.getenv("HUNT_ASSUME_ALL_TAKER", "0") == "1"
    label, scores2, r = classify_high_frequency_wallet(agg, low_freq_threshold=1, assume_all_taker=assume_taker)
    return label, scores2, r
