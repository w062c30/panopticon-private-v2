from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WalletAggFeatures:
    trade_count: int
    volume_proxy: float
    unique_markets: int
    burst_score: float


def _trade_size(tr: dict[str, Any]) -> float:
    for k in ("size", "matched_amount", "amount", "matched", "outcomeTokens"):
        v = tr.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return 0.0


def aggregate_from_observations(observations: list[dict[str, Any]]) -> WalletAggFeatures:
    """Aggregate recent ``wallet_observations`` rows (already parsed ``payload`` dict)."""
    trade_count = 0
    volume_proxy = 0.0
    markets: set[str] = set()
    for o in observations:
        if o.get("obs_type") != "clob_trade":
            continue
        # fetch_recent_wallet_observations already parses payload_json into a dict.
        # payload is directly the trade object (e.g. {"side": "BUY", "size": 13.26, ...})
        # NOT {"trade": {"side": "BUY", ...}}
        payload = o.get("payload") or {}
        tr = payload if isinstance(payload, dict) else {}
        if not tr:
            continue
        trade_count += 1
        mid = o.get("market_id")
        if isinstance(mid, str) and mid:
            markets.add(mid)
        volume_proxy += abs(_trade_size(tr))
    burst_score = min(1.0, trade_count / 25.0) if trade_count else 0.0
    return WalletAggFeatures(
        trade_count=trade_count,
        volume_proxy=volume_proxy,
        unique_markets=len(markets),
        burst_score=burst_score,
    )
