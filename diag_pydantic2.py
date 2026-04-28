import time, sys
sys.path.insert(0, '.')
import datetime
import logging

# Set up logging the same way the app might
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

# Now test: does logging slow down the loop?
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.db import ShadowDB

db = ShadowDB()
db.bootstrap()

rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)

# Time: 19 dict creations + 19 TradeItem Pydantic models
from panopticon_py.api.schemas import TradeItem
import urllib.parse

def _safe_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("none", "null", "nan", ""):
            return None
        try:
            return float(value)
        except ValueError:
            return None
    return float(value)

trades = []
for r in rows:
    trade_status = str(r.get("status") or "closed")
    from panopticon_py.polymarket.link_resolver import ResolvedPolymarketLink
    if trade_status == "open":
        link = None  # skip for now
    else:
        market_id_for_slug = r.get("market_id")
        slug_resolved = ""
        if slug_resolved:
            event_url = f"https://polymarket.com/event/{slug_resolved}"
            link = ResolvedPolymarketLink(
                event_url=event_url, embed_url=None, link_type="canonical_event",
                source="db_cache", reason="slug_from_link_map",
                market_id=market_id_for_slug, token_id=r.get("token_id"),
                event_slug=slug_resolved, market_slug=slug_resolved, event_name="",
            )
        else:
            fallback_url = f"https://polymarket.com/search?q={urllib.parse.quote(str(market_id_for_slug or ''))}"
            link = ResolvedPolymarketLink(
                event_url=fallback_url, embed_url=None, link_type="search_fallback",
                source="fallback", reason="no_slug_in_cache",
                market_id=market_id_for_slug, token_id=r.get("token_id"),
                event_slug=None, market_slug=None, event_name=None,
            )
    trade = {
        "tradeId": r["trade_id"],
        "marketId": r["market_id"],
        "eventName": str("test"),
        "eventUrl": link.event_url if link else None,
        "linkType": link.link_type if link else None,
        "linkSource": link.source if link else None,
        "linkReason": link.reason if link else None,
        "direction": r["direction"],
        "confidence": _safe_float(r["confidence"]),
        "openReason": r["open_reason"],
        "entryPrice": _safe_float(r["entry_price"]),
        "exitPrice": _safe_float(r["exit_price"]),
        "positionSizeUsd": _safe_float(r["position_size_usd"]),
        "estimatedEvUsd": _safe_float(r["estimated_ev_usd"]),
        "realizedPnlUsd": _safe_float(r["realized_pnl_usd"]),
        "unrealizedPnlUsd": _safe_float(r.get("unrealized_pnl_usd", 0.0)),
        "status": trade_status,
        "markPrice": _safe_float(r.get("mark_price")),
        "updatedAt": str(r.get("updated_at") or r.get("closed_ts_utc")),
        "closeCondition": r["close_condition"],
        "openedAt": r["opened_ts_utc"],
        "closedAt": r["closed_ts_utc"],
        "source": r.get("source", "live"),
    }
    trades.append(trade)

db.close()

# Now test with 1000 iterations
from panopticon_py.api.schemas import TradeItem

t0 = time.monotonic()
for _ in range(100):
    for t in trades:
        TradeItem(**t)
t1 = time.monotonic()
print(f'100 iterations of 19 TradeItem creations: {(t1-t0)*1000:.0f}ms')
print(f'Per TradeItem: {(t1-t0)*1000/1900:.2f}ms')
