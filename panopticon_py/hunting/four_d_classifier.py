"""4D-style entity classifier: IDI, burstiness, taker ratio — MM vs insider slicing."""

from __future__ import annotations

import json
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
    size_entropy: float = 0.0
    concentration: float = 0.0
    funding_source: float = 0.0
    insufficient_data: bool = False


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


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _extract_fingerprint_dims(fingerprint: dict | None) -> tuple[float, float, float, bool]:
    if not isinstance(fingerprint, dict):
        return 0.0, 0.0, 0.0, False
    conc = fingerprint.get("concentration")
    conc_max = conc.get("max", 0.0) if isinstance(conc, dict) else 0.0
    return (
        _clamp01(float(fingerprint.get("size_entropy") or 0.0)),
        _clamp01(float(conc_max or 0.0)),
        _clamp01(float(fingerprint.get("funding_source") or 0.0)),
        bool(fingerprint.get("insufficient_data", False)),
    )


def scores_from_parents(
    parents: list[ParentTrade],
    *,
    assume_all_taker: bool = True,
    fingerprint: dict | None = None,
) -> FourDScores:
    """IDI from signed volume proxy (side * volume), burst from inter-arrival gini, taker from flags."""
    if not parents:
        size_entropy, concentration, funding_source, insufficient_data = _extract_fingerprint_dims(
            fingerprint
        )
        return FourDScores(
            idi=0.0,
            burst=0.0,
            taker_ratio=0.0,
            size_entropy=size_entropy,
            concentration=concentration,
            funding_source=funding_source,
            insufficient_data=insufficient_data,
        )
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
    size_entropy, concentration, funding_source, insufficient_data = _extract_fingerprint_dims(
        fingerprint
    )
    return FourDScores(
        idi=idi,
        burst=burst,
        taker_ratio=taker_ratio,
        size_entropy=size_entropy,
        concentration=concentration,
        funding_source=funding_source,
        insufficient_data=insufficient_data,
    )


def _weights() -> tuple[float, float, float, float, float, float]:
    w_idi = float(os.getenv("INSIDER_W_IDI", "0.30"))
    w_burst = float(os.getenv("INSIDER_W_BURST", "0.25"))
    w_taker = float(os.getenv("INSIDER_W_TAKER", "0.20"))
    w_size = float(os.getenv("INSIDER_W_SIZE_ENT", "0.15"))
    w_conc = float(os.getenv("INSIDER_W_CONC", "0.10"))
    w_fund = float(os.getenv("INSIDER_W_FUND", "0.00"))
    total = w_idi + w_burst + w_taker + w_size + w_conc + w_fund
    if total <= 0:
        return 0.30, 0.25, 0.20, 0.15, 0.10, 0.0
    if abs(total - 1.0) < 1e-6:
        return w_idi, w_burst, w_taker, w_size, w_conc, w_fund
    return (
        w_idi / total,
        w_burst / total,
        w_taker / total,
        w_size / total,
        w_conc / total,
        w_fund / total,
    )


def compute_insider_score(scores: FourDScores) -> float:
    w_idi, w_burst, w_taker, w_size, w_conc, w_fund = _weights()
    raw = (
        (w_idi * scores.idi)
        + (w_burst * scores.burst)
        + (w_taker * scores.taker_ratio)
        + (w_size * scores.size_entropy)
        + (w_conc * scores.concentration)
        + (w_fund * scores.funding_source)
    )
    confidence_factor = 0.5 if scores.insufficient_data else 1.0
    return _clamp01(raw * confidence_factor)


def load_fingerprint_from_watchlist(wallet_address: str, db_conn) -> dict | None:
    """Load score_components_json from wallet_watchlist using existing DB connection."""
    try:
        row = db_conn.execute(
            "SELECT score_components_json FROM wallet_watchlist WHERE wallet_address=? LIMIT 1",
            (wallet_address.lower(),),
        ).fetchone()
        if not row:
            return None
        raw = row[0]
        if not raw:
            return None
        if isinstance(raw, str):
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    return None


def classify_high_frequency_wallet(
    parents: list[ParentTrade],
    *,
    low_freq_threshold: int | None = None,
    assume_all_taker: bool = False,
    fingerprint: dict | None = None,
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
    s = scores_from_parents(
        parents,
        assume_all_taker=assume_all_taker,
        fingerprint=fingerprint,
    )
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
