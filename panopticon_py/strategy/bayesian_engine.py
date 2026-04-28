"""Bayesian engine 3.0 patch: cluster Net_Delta, exposure cap, hedge exception, posterior cap."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from panopticon_py.strategy.decide_core import bayesian_update, fractional_kelly
from panopticon_py.strategy.iron_rules import ClusterExposureCapError

logger = logging.getLogger(__name__)

ClusterUnknownPolicy = Literal["unknown_bucket_5pct", "largest_cluster_rho1"]


@dataclass(frozen=True)
class PortfolioPosition:
    """Signed notional in USD: +YES exposure / conventional long risk."""

    market_id: str
    signed_notional_usd: float


@dataclass
class ClusterExposureAudit:
    cluster_id: str
    net_delta: float
    cap_usd: float
    at_or_over_cap: bool
    unknown_market: bool
    rho_notes: dict[str, Any] = field(default_factory=dict)


def _direction_from_signed_notional(x: float) -> int:
    return 1 if x >= 0 else -1


def _load_internal_direction_map(path: str | None = None) -> dict[str, int]:
    """
    Read market semantic directions from cluster_mapping.json.
    Missing or invalid rows default to +1 (conservative long-side proxy).
    """
    try:
        from panopticon_py.hunting.semantic_router import read_cluster_mapping_full

        raw = read_cluster_mapping_full(path or os.getenv("CLUSTER_MAPPING_PATH"))
    except Exception:
        return {}
    out: dict[str, int] = {}
    for market_id, row in raw.items():
        if not isinstance(market_id, str) or not isinstance(row, dict):
            continue
        try:
            dv = int(row.get("internal_direction", 1))
        except (TypeError, ValueError):
            dv = 1
        out[market_id] = -1 if dv < 0 else 1
    return out


def _inject_cluster_semantic_rho_fallback(
    *,
    target_market: str,
    target_cluster: str,
    portfolio: list[PortfolioPosition],
    cluster_map: dict[str, str],
    correlation_matrix: dict[tuple[str, str], float],
) -> dict[tuple[str, str], float]:
    """
    Deterministic rho fallback:
    - same cluster -> +1.0 or -1.0 (by target internal_direction vs active position direction)
    - different cluster -> 0.0
    Existing matrix entries are preserved.
    """
    out = dict(correlation_matrix)
    internal_dirs = _load_internal_direction_map()
    target_dir = internal_dirs.get(target_market, 1)
    for p in portfolio:
        peer_market = p.market_id
        if not peer_market:
            continue
        key = (peer_market, target_market)
        rev = (target_market, peer_market)
        if key in out or rev in out:
            continue
        peer_cluster = cluster_map.get(peer_market, "UNKNOWN_CLUSTER")
        if target_cluster == "UNKNOWN_CLUSTER" or peer_cluster == "UNKNOWN_CLUSTER":
            continue
        if peer_cluster == target_cluster:
            peer_dir = _direction_from_signed_notional(float(p.signed_notional_usd))
            rho = 1.0 if peer_dir == target_dir else -1.0
        else:
            rho = 0.0
        out[key] = rho
        out[rev] = rho
    return out


def _get_rho(
    market_a: str,
    market_b: str,
    correlation_matrix: dict[tuple[str, str], float],
) -> float:
    """Missing matrix entries default to 1.0 (never 0)."""
    if market_a == market_b:
        return 1.0
    key = (market_a, market_b)
    if key in correlation_matrix:
        return float(correlation_matrix[key])
    rev = (market_b, market_a)
    if rev in correlation_matrix:
        return float(correlation_matrix[rev])
    return 1.0


def largest_cluster_by_abs_net(
    portfolio: list[PortfolioPosition],
    cluster_map: dict[str, str],
) -> str | None:
    """Pick cluster with max |aggregated simple sum| for UNKNOWN fallback logging."""
    by_c: dict[str, float] = {}
    for p in portfolio:
        cid = cluster_map.get(p.market_id)
        if cid is None:
            continue
        by_c[cid] = by_c.get(cid, 0.0) + p.signed_notional_usd
    if not by_c:
        return None
    return max(by_c.keys(), key=lambda k: abs(by_c[k]))


def net_delta_for_cluster(
    cluster_id: str,
    portfolio: list[PortfolioPosition],
    cluster_map: dict[str, str],
    cluster_anchor: dict[str, str],
    correlation_matrix: dict[tuple[str, str], float],
) -> tuple[float, dict[str, Any]]:
    """
    Net_Delta = sum_i position_i * rho( market_i , anchor(cluster) ).
    Markets not mapped to this cluster contribute 0.
    """
    anchor = cluster_anchor.get(cluster_id)
    notes: dict[str, Any] = {"anchor": anchor, "terms": []}
    total = 0.0
    for p in portfolio:
        if cluster_map.get(p.market_id) != cluster_id:
            continue
        ref = anchor or p.market_id
        rho = _get_rho(p.market_id, ref, correlation_matrix)
        term = p.signed_notional_usd * rho
        total += term
        notes["terms"].append({"market_id": p.market_id, "signed": p.signed_notional_usd, "rho": rho, "term": term})
    return total, notes


def resolve_target_cluster(
    target_market: str,
    cluster_map: dict[str, str],
    portfolio: list[PortfolioPosition],
    unknown_policy: ClusterUnknownPolicy,
) -> tuple[str, bool]:
    """
    Returns (cluster_id, is_unknown).
    UNKNOWN: assign UNKNOWN_CLUSTER bucket or tie to largest cluster per policy (audit only for largest path).
    """
    if target_market in cluster_map:
        return cluster_map[target_market], False
    if unknown_policy == "largest_cluster_rho1":
        lc = largest_cluster_by_abs_net(portfolio, cluster_map)
        if lc is not None:
            logger.warning(
                "UNKNOWN_MARKET_FALLBACK rho=1.0 vs largest_cluster=%s market=%s",
                lc,
                target_market,
            )
            return lc, True
    return "UNKNOWN_CLUSTER", True


def check_cluster_exposure_limit(
    target_market: str,
    proposed_signed_notional_usd: float,
    portfolio: list[PortfolioPosition],
    cluster_map: dict[str, str],
    cluster_anchor: dict[str, str],
    correlation_matrix: dict[tuple[str, str], float],
    *,
    total_capital_usd: float,
    cluster_cap_fraction: float = 0.25,
    unknown_individual_cap_fraction: float = 0.05,
    unknown_policy: ClusterUnknownPolicy = "unknown_bucket_5pct",
) -> tuple[bool, str, ClusterExposureAudit]:
    """
    If |Net_Delta| >= cap after proposed trade (non-hedge), reject.
    Hedge: allow iff |Net_Delta_after| < |Net_Delta_before| for the governing cluster.
    UNKNOWN markets: strict 5% individual cap on |pos_after| for that market when unknown_bucket_5pct.
    """
    cap_usd = abs(float(total_capital_usd)) * float(cluster_cap_fraction)
    unk_cap = abs(float(total_capital_usd)) * float(unknown_individual_cap_fraction)

    cluster_id, is_unknown = resolve_target_cluster(
        target_market, cluster_map, portfolio, unknown_policy
    )

    cluster_map_eff = dict(cluster_map)
    if is_unknown and unknown_policy == "largest_cluster_rho1":
        cluster_map_eff[target_market] = cluster_id

    if is_unknown and unknown_policy == "unknown_bucket_5pct":
        cur = sum(p.signed_notional_usd for p in portfolio if p.market_id == target_market)
        after = cur + proposed_signed_notional_usd
        if abs(after) > unk_cap + 1e-9:
            audit = ClusterExposureAudit(
                cluster_id="UNKNOWN_CLUSTER",
                net_delta=after,
                cap_usd=unk_cap,
                at_or_over_cap=True,
                unknown_market=True,
                rho_notes={"reason": "unknown_market_5pct_cap", "after": after},
            )
            return False, "UNKNOWN_CLUSTER_5PCT_CAP", audit

    corr_eff = _inject_cluster_semantic_rho_fallback(
        target_market=target_market,
        target_cluster=cluster_id,
        portfolio=portfolio,
        cluster_map=cluster_map_eff,
        correlation_matrix=correlation_matrix,
    )

    net_before, notes_b = net_delta_for_cluster(
        cluster_id, portfolio, cluster_map_eff, cluster_anchor, corr_eff
    )
    proposed_portfolio = list(portfolio) + [
        PortfolioPosition(market_id=target_market, signed_notional_usd=proposed_signed_notional_usd)
    ]
    net_after, notes_a = net_delta_for_cluster(
        cluster_id, proposed_portfolio, cluster_map_eff, cluster_anchor, corr_eff
    )

    audit = ClusterExposureAudit(
        cluster_id=cluster_id,
        net_delta=net_after,
        cap_usd=cap_usd,
        at_or_over_cap=abs(net_after) >= cap_usd - 1e-12,
        unknown_market=is_unknown,
        rho_notes={"before": net_before, "after": net_after, "notes_before": notes_b, "notes_after": notes_a},
    )

    if abs(net_after) < abs(net_before) - 1e-12:
        logger.info(
            "HEDGE_EXCEPTION_ALLOWED cluster=%s |net| %s -> %s",
            cluster_id,
            abs(net_before),
            abs(net_after),
            extra={"audit": audit.__dict__},
        )
        return True, "HEDGE_REDUCES_ABS_NET_DELTA", audit

    if abs(net_after) > cap_usd + 1e-9:
        return False, "CLUSTER_EXPOSURE_CAP", audit

    return True, "OK", audit


def enforce_cluster_limit_or_raise(
    target_market: str,
    proposed_signed_notional_usd: float,
    portfolio: list[PortfolioPosition],
    cluster_map: dict[str, str],
    cluster_anchor: dict[str, str],
    correlation_matrix: dict[tuple[str, str], float],
    *,
    total_capital_usd: float,
    cluster_cap_fraction: float = 0.25,
) -> ClusterExposureAudit:
    ok, reason, audit = check_cluster_exposure_limit(
        target_market,
        proposed_signed_notional_usd,
        portfolio,
        cluster_map,
        cluster_anchor,
        correlation_matrix,
        total_capital_usd=total_capital_usd,
        cluster_cap_fraction=cluster_cap_fraction,
    )
    if not ok:
        raise ClusterExposureCapError(f"{reason} market={target_market!r} audit={audit.__dict__!r}")
    return audit


@dataclass
class BayesianEngineConfig:
    lr_entropy: float = 5.0
    entropy_z_threshold: float = -4.0
    posterior_cap: float = 0.99
    kelly_alpha: float = 0.25
    fee_rate: float = 0.0
    slippage_pct: float = 0.0
    cluster_cap_fraction: float = 0.25
    unknown_individual_cap_fraction: float = 0.05
    unknown_policy: ClusterUnknownPolicy = "unknown_bucket_5pct"


class BayesianEngine:
    """
    Injects cluster_mapping + correlation_matrix; calculates posterior with consensus LR
    and optional cluster gate before Kelly.
    """

    def __init__(
        self,
        cluster_mapping: dict[str, str],
        correlation_matrix: dict[tuple[str, str], float],
        *,
        cluster_anchor: dict[str, str] | None = None,
        config: BayesianEngineConfig | None = None,
    ) -> None:
        self.cluster_mapping = dict(cluster_mapping)
        self.correlation_matrix = dict(correlation_matrix)
        self.cluster_anchor = dict(cluster_anchor or {})
        self.config = config or BayesianEngineConfig()

    def calculate_posterior(
        self,
        prior_p: float,
        entropy_z_score: float | None,
        consensus_k_eff: float,
        *,
        use_entropy_lr: bool = True,
    ) -> tuple[float, dict[str, Any]]:
        lr_c = math.pow(2.0, float(consensus_k_eff))
        lr_e = self.config.lr_entropy if (
            use_entropy_lr and entropy_z_score is not None and float(entropy_z_score) < self.config.entropy_z_threshold
        ) else 1.0
        lr = lr_e * lr_c
        raw = bayesian_update(float(prior_p), lr)
        capped = min(float(self.config.posterior_cap), max(1e-6, raw))
        audit = {
            "lr_entropy": lr_e,
            "lr_consensus": lr_c,
            "lr_total": lr,
            "posterior_raw": raw,
            "posterior_capped": capped,
        }
        return capped, audit

    def gate_consensus_signal(
        self,
        *,
        target_market: str,
        proposed_signed_notional_usd: float,
        portfolio: list[PortfolioPosition],
        total_capital_usd: float,
    ) -> None:
        """Hard gate: raises ClusterExposureCapError when new flow increases risk over cap."""
        enforce_cluster_limit_or_raise(
            target_market,
            proposed_signed_notional_usd,
            portfolio,
            self.cluster_mapping,
            self.cluster_anchor,
            self.correlation_matrix,
            total_capital_usd=total_capital_usd,
            cluster_cap_fraction=self.config.cluster_cap_fraction,
        )

    def size_after_friction(
        self,
        posterior: float,
        price: float,
    ) -> tuple[float, float]:
        kelly = fractional_kelly(posterior, price, self.config.kelly_alpha)
        return kelly * max(0.0, 1.0 - self.config.fee_rate - self.config.slippage_pct), kelly


def load_cluster_mapping_for_engine(path: str | None = None) -> dict[str, str]:
    """
    Load ``market_id -> cluster_id`` from ``cluster_mapping.json`` (written by ``Market_Semantic_Router``).

    Delegates to ``panopticon_py.hunting.semantic_router.load_cluster_mapping_for_engine``.
    Default file: env ``CLUSTER_MAPPING_PATH`` or ``data/cluster_mapping.json``.

    If a market id is **not** present in the returned map, ``resolve_target_cluster`` treats it as
    unknown: ``UNKNOWN_CLUSTER`` with **strict 5%** notional cap when ``unknown_policy=unknown_bucket_5pct``,
    and correlation defaults (``_get_rho``) use **rho = 1.0** (never 0).
    """
    from panopticon_py.hunting.semantic_router import load_cluster_mapping_for_engine as _load_from_file

    return _load_from_file(path)


def load_engine_from_env(
    cluster_mapping: dict[str, str],
    correlation_matrix: dict[tuple[str, str], float],
    **kwargs: Any,
) -> BayesianEngine:
    cfg = BayesianEngineConfig(
        lr_entropy=float(os.getenv("BAYES_LR_ENTROPY", "5.0")),
        entropy_z_threshold=float(os.getenv("BAYES_ENTROPY_Z_THRESHOLD", "-4.0")),
        posterior_cap=float(os.getenv("POSTERIOR_CAP", "0.99")),
        kelly_alpha=float(os.getenv("KELLY_ALPHA", "0.25")),
        cluster_cap_fraction=float(os.getenv("CLUSTER_CAP_FRACTION", "0.25")),
        unknown_individual_cap_fraction=float(os.getenv("UNKNOWN_MARKET_CAP_FRACTION", "0.05")),
    )
    return BayesianEngine(cluster_mapping, correlation_matrix, config=cfg, **kwargs)
