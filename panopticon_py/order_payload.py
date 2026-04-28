from __future__ import annotations

from typing import Any, Literal


def build_protected_limit_dict(
    *,
    side: Literal["BUY", "SELL"],
    quote_price: float,
    order_size: float,
    slippage_tolerance: float,
    kyle_lambda: float,
    bid_ask_imbalance: float = 0.0,
    bai_multiplier: float = 3.0,
    ttl_seconds: int = 10,
) -> dict[str, Any]:
    """
    Mirror panopticon_ts/src/orderPayload.ts buildProtectedLimitPayload (FOK, ttl cap 10).

    Slippage estimate is consistent with fast_gate.py:
      expected_slippage = order_size * kyle_lambda * (1 + bai_multiplier * bai)
    where bai = bid_ask_imbalance is in [0, 1].
    """
    ttl = min(10, max(1, ttl_seconds))
    bai = max(0.0, min(1.0, bid_ask_imbalance))
    expected_slippage = order_size * kyle_lambda * (1.0 + bai_multiplier * bai)
    price = quote_price + max(0.0, slippage_tolerance)
    expected_avg_price = quote_price + expected_slippage
    return {
        "side": side,
        "price": price,
        "size": order_size,
        "time_in_force": "FOK",
        "expires_in_seconds": ttl,
        "expected_avg_price": expected_avg_price,
        "slippage_tolerance": slippage_tolerance,
    }
