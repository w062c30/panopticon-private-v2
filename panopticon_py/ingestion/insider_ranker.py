from __future__ import annotations

from panopticon_py.ingestion.wallet_features import WalletAggFeatures


def rank_insider(feats: WalletAggFeatures) -> tuple[float, list[str]]:
    """Heuristic 0..1 insider-risk style score with human-readable reasons."""
    reasons: list[str] = []
    score = 0.0
    if feats.trade_count >= 8:
        score += 0.28
        reasons.append("high_trade_count")
    elif feats.trade_count >= 4:
        score += 0.12
        reasons.append("elevated_trade_count")

    if feats.volume_proxy >= 5000.0:
        score += 0.22
        reasons.append("large_notional_proxy")
    elif feats.volume_proxy >= 1500.0:
        score += 0.1
        reasons.append("medium_notional_proxy")

    if feats.unique_markets >= 2:
        score += 0.18
        reasons.append("multi_market_activity")

    score += 0.22 * feats.burst_score
    if feats.burst_score >= 0.45:
        reasons.append("burst_activity")

    return min(1.0, score), reasons
