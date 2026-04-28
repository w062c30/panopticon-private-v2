from __future__ import annotations

import math
import time

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def pearson_rho(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 5:
        return None
    a = a[-n:]
    b = b[-n:]
    ma, mb = _mean(a), _mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    if da < 1e-12 or db < 1e-12:
        return None
    return max(-1.0, min(1.0, num / (da * db)))


def pairwise_correlation_edges(
    series_by_market: dict[str, list[float]],
    *,
    window_sec: int,
    epsilon: float = 0.01,
) -> list[dict[str, object]]:
    """Emit sparse edges for |rho| >= epsilon (canonical market_a < market_b)."""
    markets = sorted(series_by_market.keys())
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out: list[dict[str, object]] = []
    for i, ma in enumerate(markets):
        for mb in markets[i + 1 :]:
            rho = pearson_rho(series_by_market[ma], series_by_market[mb])
            if rho is None or abs(rho) < epsilon:
                continue
            a, b = (ma, mb) if ma < mb else (mb, ma)
            out.append({"market_a": a, "market_b": b, "rho": float(rho), "window_sec": window_sec, "updated_ts_utc": ts})
    return out


def align_series(series_by_market: dict[str, list[float]]) -> dict[str, list[float]]:
    m = min((len(v) for v in series_by_market.values()), default=0)
    if m <= 0:
        return {}
    return {k: v[-m:] for k, v in series_by_market.items()}
