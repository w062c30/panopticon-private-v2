"""Trade aggregation: taker sweeps (single wallet) and cross-wallet burst clusters (smurfing)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import uuid4


@dataclass
class ParentTrade:
    """Synthetic parent order after aggregation."""

    taker: str
    side: int  # +1 buy, -1 sell (YES outcome convention)
    volume: float
    first_ts_ms: float
    last_ts_ms: float
    child_count: int
    market_id: str | None = None


@dataclass
class VirtualEntity:
    """Temporary cross-wallet coordinated burst."""

    entity_id: str
    members: list[str] = field(default_factory=list)
    side: int = 1
    total_volume: float = 0.0
    first_ts_ms: float = 0.0
    last_ts_ms: float = 0.0
    trade_count: int = 0


def _ts_ms(tr: dict[str, Any]) -> float:
    for k in ("timestamp", "match_time", "created_at", "last_update", "ts"):
        v = tr.get(k)
        if isinstance(v, (int, float)):
            x = float(v)
            return x * 1000.0 if x < 1e12 else x  # seconds vs ms heuristic
        if isinstance(v, str):
            try:
                xf = float(v)
                return xf * 1000.0 if xf < 1e12 else xf
            except ValueError:
                continue
    return 0.0


def _taker(tr: dict[str, Any]) -> str:
    for k in ("taker", "taker_address", "trader", "address"):
        v = tr.get(k)
        if isinstance(v, str) and v.startswith("0x"):
            return v.lower()[:42]
        if isinstance(v, dict) and isinstance(v.get("address"), str):
            return str(v["address"]).lower()[:42]
    return ""


def _side(tr: dict[str, Any]) -> int:
    s = tr.get("side")
    if s in ("BUY", "buy", "BUY_YES", 1, "1"):
        return 1
    if s in ("SELL", "sell", "SELL_YES", -1, "-1"):
        return -1
    # Polymarket sometimes uses outcome index
    if isinstance(tr.get("outcomeIndex"), int):
        return 1 if tr["outcomeIndex"] == 0 else -1
    return 1


def _size(tr: dict[str, Any]) -> float:
    for k in ("size", "amount", "matched_amount"):
        v = tr.get(k)
        if isinstance(v, (int, float)):
            return abs(float(v))
        if isinstance(v, str):
            try:
                return abs(float(v))
            except ValueError:
                continue
    return 0.0


def _market_id(tr: dict[str, Any]) -> str | None:
    for k in ("market", "market_id", "condition_id", "token_id", "asset_id"):
        v = tr.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def aggregate_taker_sweeps(
    trades: Iterable[dict[str, Any]],
    *,
    gap_ms: float | None = None,
) -> list[ParentTrade]:
    """
    Merge consecutive fills with same taker, same side, timestamps within ``gap_ms``.
    """
    gap = float(gap_ms if gap_ms is not None else os.getenv("HUNT_SWEEP_GAP_MS", "10"))
    rows = sorted(trades, key=_ts_ms)
    out: list[ParentTrade] = []
    buf: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        t0 = _ts_ms(buf[0])
        t1 = _ts_ms(buf[-1])
        tk = _taker(buf[0])
        sd = _side(buf[0])
        mid = _market_id(buf[0])
        vol = sum(_size(x) for x in buf)
        out.append(
            ParentTrade(
                taker=tk,
                side=sd,
                volume=vol,
                first_ts_ms=t0,
                last_ts_ms=t1,
                child_count=len(buf),
                market_id=mid,
            )
        )
        buf = []

    for tr in rows:
        if not _taker(tr):
            continue
        if not buf:
            buf.append(tr)
            continue
        same_taker = _taker(tr) == _taker(buf[0])
        same_side = _side(tr) == _side(buf[0])
        dt = _ts_ms(tr) - _ts_ms(buf[-1])
        same_market = (_market_id(tr) or "") == (_market_id(buf[0]) or "") or not _market_id(buf[0])
        if same_taker and same_side and same_market and 0 <= dt <= gap:
            buf.append(tr)
        else:
            flush()
            buf.append(tr)
    flush()
    return out


def cross_wallet_burst_cluster(
    trades: Iterable[dict[str, Any]],
    *,
    max_inter_trade_ms: float | None = None,
    min_cluster_size: int | None = None,
) -> tuple[list[ParentTrade], list[VirtualEntity]]:
    """
    After per-wallet sweeps, detect bursts of taker trades across *different* wallets.

    Returns (per_wallet_parents_aggregated_again_flattened_not_used — simplified:
    returns (list of single-wallet parents from full stream, list of VirtualEntity).

    Implementation: sort all trades; walk with sliding window on inter-arrival;
    if >= min_cluster_size distinct takers within max_inter_trade_ms chain, emit VirtualEntity.
    """
    inter = float(max_inter_trade_ms if max_inter_trade_ms is not None else os.getenv("HUNT_CROSS_WALLET_MS", "50"))
    min_n = int(min_cluster_size if min_cluster_size is not None else os.getenv("HUNT_CROSS_WALLET_MIN_N", "3"))
    rows = sorted(trades, key=_ts_ms)
    singles = aggregate_taker_sweeps(rows)
    virtuals: list[VirtualEntity] = []
    if len(rows) < min_n:
        return singles, virtuals

    # Greedy clusters: extend chain while next trade within inter ms and same side/market
    i = 0
    n = len(rows)
    while i < n:
        cluster: list[dict[str, Any]] = [rows[i]]
        j = i + 1
        while j < n:
            if _ts_ms(rows[j]) - _ts_ms(rows[j - 1]) > inter:
                break
            if _side(rows[j]) != _side(cluster[0]):
                break
            if (_market_id(rows[j]) or "") != (_market_id(cluster[0]) or "") and _market_id(cluster[0]):
                break
            cluster.append(rows[j])
            j += 1
        takers = {_taker(t) for t in cluster if _taker(t)}
        if len(cluster) >= min_n and len(takers) >= min_n:
            vol = sum(_size(t) for t in cluster)
            ve = VirtualEntity(
                entity_id=str(uuid4()),
                members=sorted(takers),
                side=_side(cluster[0]),
                total_volume=vol,
                first_ts_ms=_ts_ms(cluster[0]),
                last_ts_ms=_ts_ms(cluster[-1]),
                trade_count=len(cluster),
            )
            virtuals.append(ve)
            i = j
        else:
            i += 1
    return singles, virtuals
