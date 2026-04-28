from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from panopticon_py.db import ShadowDB
from panopticon_py.ingestion.clob_client import fetch_trades


# ── Module-level market-price cache (shared across requests, 30s TTL) ───────────
_price_cache: dict[str, tuple[float, float]] = {}  # token_id -> (price, timestamp)
_price_lock = threading.Lock()


def _get_cached_price(token_id: str, max_age_sec: float = 30.0) -> float | None:
    """Return cached price if fresh enough, else None."""
    with _price_lock:
        entry = _price_cache.get(token_id)
        if entry is None:
            return None
        price, cached_at = entry
        if time.monotonic() - cached_at > max_age_sec:
            return None
        return price


def _set_cached_price(token_id: str, price: float) -> None:
    with _price_lock:
        _price_cache[token_id] = (price, time.monotonic())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def token_ids_from_env() -> list[str]:
    import os

    raw = os.getenv("PANOPTICON_CLOB_TOKEN_IDS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    one = os.getenv("PANOPTICON_CLOB_TOKEN_ID", "").strip()
    return [one] if one else []


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_iso(ts_val: Any) -> str:
    if isinstance(ts_val, str) and ts_val:
        return ts_val
    if isinstance(ts_val, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts_val), tz=timezone.utc).isoformat()
        except Exception:
            pass
    return _utc_now().isoformat()


def _period_cutoff(period: str) -> datetime | None:
    now = _utc_now()
    if period == "1d":
        return now - timedelta(days=1)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    return None


def _filter_by_period(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    cutoff = _period_cutoff(period)
    if cutoff is None:
        return rows
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(str(r["closed_ts_utc"]).replace("Z", "+00:00"))
        except Exception:
            continue
        if ts >= cutoff:
            out.append(r)
    return out


def fetch_live_trade_rows(limit: int = 20) -> list[dict[str, Any]]:
    token_ids = token_ids_from_env()
    if not token_ids:
        return []

    all_rows: list[dict[str, Any]] = []
    for token_id in token_ids:
        trades = fetch_trades(token_id, limit=max(40, limit * 2))
        if not trades:
            continue
        if len(trades) < 2:
            continue
        trades_sorted = sorted(trades, key=lambda t: _to_iso(t.get("timestamp") or t.get("createdAt") or t.get("time")))
        for idx, tr in enumerate(trades_sorted[1:], start=1):
            prev = trades_sorted[idx - 1]
            current_price = _to_float(tr.get("price") or tr.get("outcomePrice"))
            prev_price = _to_float(prev.get("price") or prev.get("outcomePrice"), current_price)
            size = max(_to_float(tr.get("size") or tr.get("amount"), 25.0), 1.0)
            pnl = (current_price - prev_price) * size
            ts = _to_iso(tr.get("timestamp") or tr.get("createdAt") or tr.get("time"))
            side = "YES" if current_price >= 0.5 else "NO"
            all_rows.append(
                {
                    "trade_id": str(tr.get("id") or tr.get("trade_id") or f"{token_id}-{idx}"),
                    "market_id": str(tr.get("market") or tr.get("conditionId") or token_id),
                    "token_id": token_id,
                    "event_name": str(tr.get("title") or tr.get("question") or f"Polymarket {token_id[:8]}"),
                    "direction": side,
                    "confidence": max(0.01, min(0.99, current_price)),
                    "open_reason": "POLYMARKET_LIVE_TRADE",
                    "entry_price": prev_price,
                    "exit_price": current_price,
                    "position_size_usd": size,
                    "estimated_ev_usd": pnl,
                    "realized_pnl_usd": pnl,
                    "unrealized_pnl_usd": 0.0,
                    "status": "closed",
                    "mark_price": current_price,
                    "updated_at": ts,
                    "close_condition": "live_trade_tick",
                    "opened_ts_utc": _to_iso(prev.get("timestamp") or prev.get("createdAt") or prev.get("time")),
                    "closed_ts_utc": ts,
                    "source": "live",
                }
            )

    all_rows.sort(key=lambda r: r["closed_ts_utc"], reverse=True)
    return all_rows[:limit]


def compute_live_performance(rows: list[dict[str, Any]], period: str) -> dict[str, Any]:
    period_rows = _filter_by_period(rows, period)
    pnls = [float(r.get("realized_pnl_usd", 0.0)) for r in period_rows]
    ests = [float(r.get("estimated_ev_usd", 0.0)) for r in period_rows]
    total_pnl = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else None
    mean_pnl = (total_pnl / len(pnls)) if pnls else 0.0
    std = (sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)) ** 0.5 if len(pnls) > 1 else 0.0
    sharpe = ((mean_pnl / std) * (len(pnls) ** 0.5)) if std > 0 else 0.0
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (gross_profit if gross_profit > 0 else None)
    slippage_gap = (sum((e - p) for e, p in zip(ests, pnls)) / len(pnls)) if pnls else None

    equity = 0.0
    peak = 0.0
    peak_idx = 0
    max_dd = 0.0
    trough_idx = 0
    dd_peak_idx = 0
    ordered = list(reversed(period_rows))
    for idx, row in enumerate(ordered):
        equity += float(row.get("realized_pnl_usd", 0.0))
        if equity > peak:
            peak = equity
            peak_idx = idx
        if peak > 0:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
                trough_idx = idx
                dd_peak_idx = peak_idx

    return {
        "total_pnl_usd": float(total_pnl),
        "win_rate": win_rate,
        "sharpe_ratio": float(sharpe),
        "max_drawdown": float(max_dd),
        "peak_ts": ordered[dd_peak_idx]["closed_ts_utc"] if ordered else None,
        "trough_ts": ordered[trough_idx]["closed_ts_utc"] if ordered else None,
        "from_trade_id": ordered[dd_peak_idx]["trade_id"] if ordered else None,
        "to_trade_id": ordered[trough_idx]["trade_id"] if ordered else None,
        "profit_factor": profit_factor,
        "slippage_gap": slippage_gap,
        "trade_count": len(period_rows),
    }


def compute_live_history(rows: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    ordered = sorted(_filter_by_period(rows, period), key=lambda r: r["closed_ts_utc"])
    cumulative = 0.0
    out: list[dict[str, Any]] = []
    for row in ordered:
        cumulative += float(row.get("realized_pnl_usd", 0.0))
        out.append({"ts": row["closed_ts_utc"], "cumulative_pnl_usd": cumulative})
    return out


def _latest_market_prices(token_ids: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for token_id in token_ids:
        cached = _get_cached_price(token_id)
        if cached is not None:
            out[token_id] = cached
            continue
        trades = fetch_trades(token_id, limit=1)
        if not trades:
            continue
        tr = trades[0]
        price = _to_float(tr.get("price") or tr.get("outcomePrice"), default=0.0)
        if price > 0:
            out[token_id] = price
            _set_cached_price(token_id, price)
    return out


def fetch_hybrid_trade_rows(db: ShadowDB, limit: int = 20, use_http_for_closed: bool = True) -> list[dict[str, Any]]:
    # D59b: use_http_for_closed=False skips CLOB API calls for closed trades.
    # Reduces latency from ~12s to <1s for closed paper trades.
    # Set to True only when live Polymarket trade data is genuinely needed.
    closed_rows: list[dict[str, Any]] = []
    if use_http_for_closed:
        closed_rows = fetch_live_trade_rows(limit=max(limit, 40))
    if not closed_rows:
        closed_rows = db.fetch_trade_list(limit=max(limit, 40), status="recent", period="all")
        for row in closed_rows:
            row.setdefault("token_id", row.get("market_id"))
            row.setdefault("status", "closed")
            row.setdefault("unrealized_pnl_usd", 0.0)
            row.setdefault("mark_price", row.get("exit_price"))
            row.setdefault("updated_at", row.get("closed_ts_utc"))

    open_positions = db.fetch_open_positions()
    token_ids = list({str(r.get("token_id") or r.get("market_id")) for r in closed_rows if r.get("token_id") or r.get("market_id")})
    token_ids.extend([str(p.get("market_id")) for p in open_positions if p.get("market_id")])
    # D59b: only fetch prices if there are open positions (live prices needed).
    # For closed-only requests, use exit_price as mark_price (already set above).
    prices: dict[str, float] = {}
    if open_positions:
        prices = _latest_market_prices(list(dict.fromkeys(token_ids)))

    open_rows: list[dict[str, Any]] = []
    for pos in open_positions:
        market_id = str(pos.get("market_id"))
        signed_notional = float(pos.get("signed_notional_usd", 0.0))
        side = str(pos.get("side") or ("YES" if signed_notional >= 0 else "NO"))
        size = abs(signed_notional) if signed_notional != 0 else 50.0
        baseline = 0.5
        mark = prices.get(market_id, baseline)
        if side == "YES":
            unrealized = (mark - baseline) * size
        else:
            unrealized = (baseline - mark) * size
        open_rows.append(
            {
                "trade_id": f"open-{pos.get('position_id')}",
                "market_id": market_id,
                "token_id": market_id,
                "event_name": db.resolve_slug(market_id),
                "direction": side,
                "confidence": max(0.01, min(0.99, mark)),
                "open_reason": "OPEN_POSITION_TRACKING",
                "entry_price": baseline,
                "exit_price": None,
                "position_size_usd": size,
                "estimated_ev_usd": unrealized,
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": unrealized,
                "status": "open",
                "mark_price": mark,
                "updated_at": _utc_now().isoformat(),
                "close_condition": "open_position",
                "opened_ts_utc": str(pos.get("opened_ts_utc") or _utc_now().isoformat()),
                "closed_ts_utc": str(pos.get("opened_ts_utc") or _utc_now().isoformat()),
                "source": "open_position",
            }
        )

    rows = [*open_rows, *closed_rows]
    rows.sort(key=lambda r: str(r.get("updated_at") or r.get("closed_ts_utc") or ""), reverse=True)
    return rows[:limit]


def build_live_report(rows: list[dict[str, Any]], *, canonical_hit_rate: float, fallback_rate: float, unresolved_count: int) -> dict[str, Any]:
    open_rows = [r for r in rows if str(r.get("status")) == "open"]
    closed_rows = [r for r in rows if str(r.get("status")) == "closed"]
    realized_total = sum(float(r.get("realized_pnl_usd", 0.0)) for r in rows)
    unrealized_total = sum(float(r.get("unrealized_pnl_usd", 0.0)) for r in rows)
    unique_markets = len({str(r.get("market_id")) for r in rows if r.get("market_id")})

    findings: list[str] = []
    if fallback_rate > 0.5:
        findings.append("fallback 連結比例偏高，建議補齊 canonical slug 映射。")
    if open_rows and unrealized_total < 0:
        findings.append("未實現損益為負，請關注現時持倉風險。")
    if unresolved_count > 0:
        findings.append(f"仍有 {unresolved_count} 筆事件未解析，已進入 backfill 佇列。")
    if not findings:
        findings.append("資料流健康，暫未發現顯著異常。")

    return {
        "counts": {
            "openTrades": len(open_rows),
            "closedTrades": len(closed_rows),
            "uniqueMarkets": unique_markets,
            "canonicalHitRate": canonical_hit_rate,
        },
        "pnl": {
            "realizedTotalUsd": realized_total,
            "unrealizedTotalUsd": unrealized_total,
            "netTotalUsd": realized_total + unrealized_total,
        },
        "quality": {
            "fallbackRate": fallback_rate,
            "unresolvedCount": unresolved_count,
        },
        "findings": findings,
        "updatedAt": _utc_now().isoformat(),
    }

