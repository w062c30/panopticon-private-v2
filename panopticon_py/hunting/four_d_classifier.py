"""4D-style entity classifier: IDI, burstiness, taker ratio — MM vs insider slicing."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Literal
from panopticon_py.time_utils import utc_now_rfc3339_ms

from panopticon_py.hunting.trade_aggregate import ParentTrade, VirtualEntity
logger = logging.getLogger(__name__)
_MAX_INFERENCE_PAYLOAD_BYTES = 8 * 1024

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


def _safe_score01(value, default: float = 0.0) -> float:
    try:
        return _clamp01(float(value))
    except (TypeError, ValueError):
        return default


def normalize_fingerprint_payload(payload: dict | None) -> dict:
    """
    Normalize optional fingerprint payload to a schema-safe dict.

    Contract:
    - funding_source is optional and currently may be absent from producers.
    - absent/invalid funding_source defaults to 0.0 (consumer-safe fallback).
    - scoring consumes only normalized float values in [0, 1].
    """
    if not isinstance(payload, dict):
        return {
            "size_entropy": 0.0,
            "timing_entropy": 0.0,
            "concentration": {"max": 0.0, "all_unknown": True},
            "funding_source": 0.0,
            "insufficient_data": False,
        }

    conc_raw = payload.get("concentration")
    conc_norm = {"max": 0.0}
    if isinstance(conc_raw, dict):
        conc_norm = dict(conc_raw)
        conc_norm["max"] = _safe_score01(conc_raw.get("max", 0.0))

    normalized = dict(payload)
    normalized["size_entropy"] = _safe_score01(payload.get("size_entropy", 0.0))
    normalized["concentration"] = conc_norm
    normalized["funding_source"] = _safe_score01(payload.get("funding_source", 0.0))
    normalized["insufficient_data"] = bool(payload.get("insufficient_data", False))
    return normalized


def _extract_fingerprint_dims(fingerprint: dict | None) -> tuple[float, float, float, bool]:
    normalized = normalize_fingerprint_payload(fingerprint)
    conc = normalized.get("concentration")
    conc_max = conc.get("max", 0.0) if isinstance(conc, dict) else 0.0
    return (
        _safe_score01(normalized.get("size_entropy", 0.0)),
        _safe_score01(conc_max or 0.0),
        _safe_score01(normalized.get("funding_source", 0.0)),
        bool(normalized.get("insufficient_data", False)),
    )


def _compute_taker_ratio(parents: list[ParentTrade], assume_all_taker: bool) -> float:
    """
    Estimate taker ratio from directional imbalance.
    This is a proxy heuristic (not exchange-native taker flags).
    TODO(D174/NQ-1): calibrate soft floor from backtest baseline.
    """
    if assume_all_taker:
        return 1.0
    if not parents:
        return 0.5
    net = sum(p.side * p.volume for p in parents)
    tot = sum(p.volume for p in parents) or 1.0
    directional_ratio = abs(net) / tot
    return _clamp01(max(0.3, directional_ratio))


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
            taker_ratio=_compute_taker_ratio([], assume_all_taker),
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
    taker_ratio = _compute_taker_ratio(parents, assume_all_taker)
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
    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning("[INSIDER_WEIGHTS] invalid %s=%r fallback=%s", name, raw, default)
            return default

    raw_values = [
        _env_float("INSIDER_W_IDI", 0.30),
        _env_float("INSIDER_W_BURST", 0.25),
        _env_float("INSIDER_W_TAKER", 0.20),
        _env_float("INSIDER_W_SIZE_ENT", 0.15),
        _env_float("INSIDER_W_CONC", 0.10),
        _env_float("INSIDER_W_FUND", 0.00),
    ]
    values = [_clamp01(v) for v in raw_values]
    total = sum(values)
    if total <= 0:
        return 0.30, 0.25, 0.20, 0.15, 0.10, 0.0
    return (
        values[0] / total,
        values[1] / total,
        values[2] / total,
        values[3] / total,
        values[4] / total,
        values[5] / total,
    )


def _log_score_inference(
    wallet_address: str,
    scores: FourDScores,
    insider_score: float,
    db_conn=None,
) -> None:
    """D174 NQ-1 scaffold: persist online inference config for backtesting."""
    try:
        if db_conn is None:
            return
        w_idi, w_burst, w_taker, w_size, w_conc, w_fund = _weights()
        payload = json.dumps(
            {
                "w_idi": w_idi,
                "w_burst": w_burst,
                "w_taker": w_taker,
                "w_size": w_size,
                "w_conc": w_conc,
                "w_fund": w_fund,
                "idi": scores.idi,
                "burst": scores.burst,
                "taker_ratio": scores.taker_ratio,
                "size_entropy": scores.size_entropy,
                "concentration": scores.concentration,
                "funding_source": scores.funding_source,
                "insufficient_data": scores.insufficient_data,
                "insider_score": insider_score,
                "created_at_utc": utc_now_rfc3339_ms(),
            },
            separators=(",", ":"),
        )
        if len(payload.encode("utf-8")) > _MAX_INFERENCE_PAYLOAD_BYTES:
            payload = json.dumps(
                {
                    "idi": scores.idi,
                    "burst": scores.burst,
                    "taker_ratio": scores.taker_ratio,
                    "size_entropy": scores.size_entropy,
                    "concentration": scores.concentration,
                    "funding_source": scores.funding_source,
                    "insufficient_data": scores.insufficient_data,
                    "insider_score": insider_score,
                    "created_at_utc": utc_now_rfc3339_ms(),
                    "payload_trimmed": True,
                },
                separators=(",", ":"),
            )
        db_conn.execute(
            """
            INSERT INTO insider_score_inference_log (wallet_address, inference_payload, created_at)
            VALUES (?, ?, datetime('now'))
            """,
            ((wallet_address or "").lower()[:42], payload),
        )
    except Exception:
        pass


def compute_insider_score(
    scores: FourDScores,
    *,
    wallet_address: str = "",
    db_conn=None,
) -> float:
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
    result = _clamp01(raw * confidence_factor)
    _log_score_inference(wallet_address, scores, result, db_conn=db_conn)
    return result


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
            if isinstance(payload, dict):
                normalized = normalize_fingerprint_payload(payload)
                logger.debug(
                    "[FP_LOAD] wallet=%s keys=%s has_funding_source=%s",
                    wallet_address[:20],
                    sorted(normalized.keys()),
                    "funding_source" in normalized,
                )
                return normalized
            return None
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
