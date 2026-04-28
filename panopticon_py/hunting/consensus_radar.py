"""Consensus radar 3.0: temporal decay, liquidity-relative notional, net conviction, hybrid-ish dedup."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from panopticon_py.strategy.iron_rules import assert_filled_trade_rows

logger = logging.getLogger(__name__)

OutcomeSide = Literal["YES", "NO"]
OppositeMode = Literal["penalize", "cancel"]


def _ts_sec(tr: dict[str, Any]) -> float:
    for k in ("timestamp", "match_time", "created_at", "last_update", "ts", "trade_ts"):
        v = tr.get(k)
        if isinstance(v, (int, float)):
            x = float(v)
            return x / 1000.0 if x > 1e12 else x
        if isinstance(v, str):
            try:
                xf = float(v)
                return xf / 1000.0 if xf > 1e12 else xf
            except ValueError:
                continue
    return 0.0


def _wallet(tr: dict[str, Any]) -> str:
    for k in ("taker", "taker_address", "trader", "address", "wallet"):
        v = tr.get(k)
        if isinstance(v, str) and v.startswith("0x"):
            return v.lower()[:42]
    return ""


def _notional(tr: dict[str, Any]) -> float:
    for k in ("notional_usd", "notional", "size_usd", "usd", "size"):
        v = tr.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _outcome_side(tr: dict[str, Any]) -> OutcomeSide:
    raw = str(tr.get("outcome_side") or tr.get("side_token") or tr.get("bet") or "").upper()
    if raw in {"YES", "Y", "BUY_YES"}:
        return "YES"
    if raw in {"NO", "N", "BUY_NO"}:
        return "NO"
    side = str(tr.get("side") or "").upper()
    if side in {"BUY", "YES"}:
        return "YES"
    if side in {"SELL", "NO"}:
        return "NO"
    return "YES"


def _market_id(tr: dict[str, Any]) -> str:
    for k in ("market_id", "market", "condition_id"):
        v = tr.get(k)
        if isinstance(v, str) and v:
            return v
    return "unknown"


def _is_filled_row(tr: dict[str, Any]) -> bool:
    st = str(tr.get("status") or tr.get("order_status") or "").upper()
    if st in {"OPEN", "PENDING", "PLACED", "ORDER_PLACED", "CANCELLED", "CANCELED"}:
        return False
    if tr.get("filled") is False:
        return False
    return True


@dataclass
class ConsensusRadarConfig:
    time_window_sec: float = 86_400.0
    min_distinct_wallets: int = 3
    abs_min_usd: float = 5_000.0
    ref_pct: float = 0.02
    ref_source: Literal["oi", "vol24h"] = "oi"
    lambda_decay_per_sec: float = 1.0e-5
    hybrid_link_window_sec: float = 2.0
    opposing_ratio_threshold: float = 0.3
    opposing_mode: OppositeMode = "penalize"
    opposing_penalty_factor: float = 0.25
    min_net_directional_usd: float = 100.0


@dataclass
class ConsensusSignal:
    market_id: str
    direction: OutcomeSide
    k_raw: float
    k_hybrid: float
    w_time: float
    k_eff: float
    total_notional_usd: float
    notional_threshold_usd: float
    net_yes_usd: float
    net_no_usd: float
    audit: dict[str, Any] = field(default_factory=dict)


def _union_find_effective_wallets(
    rows: list[tuple[str, float]],
    link_window_sec: float,
) -> int:
    """Merge wallets if any pairwise trades fall within link_window (Sybil-ish proxy)."""
    wallets = [w for w, _ in rows]
    if not wallets:
        return 0
    parent: dict[str, str] = {w: w for w in wallets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    ts_list = [t for _, t in rows]
    n = len(rows)
    for i in range(n):
        for j in range(i + 1, n):
            if abs(ts_list[i] - ts_list[j]) <= link_window_sec:
                union(rows[i][0], rows[j][0])
    roots = {find(w) for w in wallets}
    return len(roots)


def _dynamic_threshold(ref_notional: float, cfg: ConsensusRadarConfig) -> float:
    return max(cfg.abs_min_usd, float(ref_notional) * cfg.ref_pct)


def monitor_basket_activity(
    basket_wallets: set[str],
    trades: list[dict[str, Any]],
    *,
    now_ts_sec: float | None = None,
    cfg: ConsensusRadarConfig | None = None,
) -> list[dict[str, Any]]:
    """Filter to basket + filled-only; returns normalized rows for downstream clustering."""
    cfg = cfg or ConsensusRadarConfig()
    assert_filled_trade_rows(trades)
    tnow = float(now_ts_sec) if now_ts_sec is not None else max((_ts_sec(t) for t in trades), default=0.0)
    out: list[dict[str, Any]] = []
    for tr in trades:
        if not _is_filled_row(tr):
            continue
        w = _wallet(tr)
        if w not in basket_wallets:
            continue
        ts = _ts_sec(tr)
        if tnow - ts > cfg.time_window_sec:
            continue
        out.append(tr)
    logger.debug("monitor_basket_activity kept=%s of=%s", len(out), len(trades))
    return out


def detect_consensus_cluster(
    trades: list[dict[str, Any]],
    *,
    liquidity_ref_usd: Callable[[str], float],
    cfg: ConsensusRadarConfig | None = None,
) -> ConsensusSignal | None:
    """
    Cluster by (market_id, direction). Applies:
    - hybrid wallet dedup via union-find on tight timestamps
    - w_time = exp(-lambda * (t_last - t_first)) on cluster trades
    - notional gate vs max(ABS_MIN, ref*PCT)
    - net conviction: opposite_notional / max(|net|, eps) > threshold -> penalize or cancel
    """
    cfg = cfg or ConsensusRadarConfig()
    assert_filled_trade_rows(trades)
    if not trades:
        return None

    by_key: dict[tuple[str, OutcomeSide], list[dict[str, Any]]] = {}
    for tr in trades:
        if not _is_filled_row(tr):
            continue
        key = (_market_id(tr), _outcome_side(tr))
        by_key.setdefault(key, []).append(tr)

    best: ConsensusSignal | None = None
    for (market_id, direction), bucket in by_key.items():
        if len(bucket) < 2:
            continue
        times = sorted(_ts_sec(t) for t in bucket)
        delta_t = max(0.0, times[-1] - times[0])
        w_time = math.exp(-cfg.lambda_decay_per_sec * delta_t)

        rows = [(_wallet(t), _ts_sec(t)) for t in bucket if _wallet(t)]
        n_raw = len({w for w, _ in rows})
        k_hybrid = float(_union_find_effective_wallets(rows, cfg.hybrid_link_window_sec))

        # Opposing flow: all filled trades on same market (caller should pass windowed trades)
        market_all = [t for t in trades if _market_id(t) == market_id and _is_filled_row(t)]
        yes_all = sum(_notional(t) for t in market_all if _outcome_side(t) == "YES")
        no_all = sum(_notional(t) for t in market_all if _outcome_side(t) == "NO")
        net_dir = yes_all - no_all
        opposite = no_all if direction == "YES" else yes_all
        denom = max(abs(net_dir), cfg.min_net_directional_usd)
        conflict_ratio = opposite / denom

        total_same = sum(_notional(t) for t in bucket)
        ref = liquidity_ref_usd(market_id)
        thr = _dynamic_threshold(ref, cfg)

        audit: dict[str, Any] = {
            "market_id": market_id,
            "direction": direction,
            "delta_t_sec": delta_t,
            "w_time": w_time,
            "k_raw_wallets": n_raw,
            "k_hybrid": k_hybrid,
            "total_notional_usd": total_same,
            "threshold_usd": thr,
            "ref_notional_usd": ref,
            "yes_notional_window": yes_all,
            "no_notional_window": no_all,
            "conflict_ratio": conflict_ratio,
        }

        if total_same < thr:
            audit["reject"] = "below_notional_threshold"
            logger.info("[CONSENSUS_SKIP] %s", audit)
            continue

        if k_hybrid < cfg.min_distinct_wallets:
            audit["reject"] = "below_wallet_count"
            logger.info("[CONSENSUS_SKIP] %s", audit)
            continue

        k_eff = k_hybrid * w_time
        if conflict_ratio > cfg.opposing_ratio_threshold:
            if cfg.opposing_mode == "cancel":
                audit["reject"] = "net_conviction_cancel"
                logger.warning("[CONSENSUS_CANCEL] %s", audit)
                continue
            k_eff *= cfg.opposing_penalty_factor
            audit["penalty_applied"] = cfg.opposing_penalty_factor

        sig = ConsensusSignal(
            market_id=market_id,
            direction=direction,
            k_raw=float(n_raw),
            k_hybrid=k_hybrid,
            w_time=w_time,
            k_eff=k_eff,
            total_notional_usd=total_same,
            notional_threshold_usd=thr,
            net_yes_usd=yes_all,
            net_no_usd=no_all,
            audit=audit,
        )
        logger.info("[CONSENSUS_SIGNAL] %s", sig.audit)
        if best is None or sig.k_eff > best.k_eff:
            best = sig
    return best


def make_liquidity_ref_fn(
    oi_by_market: dict[str, float],
    vol24h_by_market: dict[str, float],
    cfg: ConsensusRadarConfig,
) -> Callable[[str], float]:
    """Build ref_notional getter for OI vs 24h volume (caller supplies snapshots)."""

    def _ref(market_id: str) -> float:
        if cfg.ref_source == "vol24h":
            return float(vol24h_by_market.get(market_id, 0.0))
        return float(oi_by_market.get(market_id, 0.0))

    return _ref


def load_consensus_config_from_env() -> ConsensusRadarConfig:
    rs = os.getenv("CONSENSUS_REF_SOURCE", "oi").lower()
    ref_source: Literal["oi", "vol24h"] = "vol24h" if rs == "vol24h" else "oi"
    om = os.getenv("CONSENSUS_OPPOSING_MODE", "penalize").lower()
    opposing_mode: OppositeMode = "cancel" if om == "cancel" else "penalize"
    return ConsensusRadarConfig(
        time_window_sec=float(os.getenv("CONSENSUS_TIME_WINDOW_SEC", "86400")),
        min_distinct_wallets=int(os.getenv("CONSENSUS_MIN_WALLETS", "3")),
        abs_min_usd=float(os.getenv("CONSENSUS_ABS_MIN_USD", "5000")),
        ref_pct=float(os.getenv("CONSENSUS_REF_PCT", "0.02")),
        ref_source=ref_source,
        lambda_decay_per_sec=float(os.getenv("CONSENSUS_LAMBDA_DECAY_PER_SEC", "1e-5")),
        hybrid_link_window_sec=float(os.getenv("CONSENSUS_HYBRID_LINK_WINDOW_SEC", "2.0")),
        opposing_ratio_threshold=float(os.getenv("CONSENSUS_OPPOSING_RATIO", "0.3")),
        opposing_mode=opposing_mode,
        opposing_penalty_factor=float(os.getenv("CONSENSUS_OPPOSING_PENALTY", "0.25")),
        min_net_directional_usd=float(os.getenv("CONSENSUS_MIN_NET_DIR_USD", "100")),
    )
