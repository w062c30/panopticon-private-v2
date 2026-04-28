import time, sys
sys.path.insert(0, '.')
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.link_resolver import resolve_polymarket_link, ResolvedPolymarketLink
from panopticon_py.api.schemas import RecommendationsResponse, TradeItem
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

db = ShadowDB()
db.bootstrap()

rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)
token_ids = [r.get("market_id") for r in rows if r.get("market_id")]
slug_map = db.batch_resolve_slugs(token_ids)

trades = []
for r in rows:
    trade_status = str(r.get("status") or "closed")
    if trade_status == "open":
        link = resolve_polymarket_link(
            db,
            market_id=r.get("market_id"),
            token_id=r.get("token_id"),
            event_name=r.get("event_name"),
        )
        resolved_name = link.event_name or r.get("event_name") or r.get("eventName", "")
    else:
        market_id_for_slug = r.get("market_id")
        slug_resolved = slug_map.get(market_id_for_slug, "") if market_id_for_slug else ""
        resolved_name = slug_resolved if slug_resolved else (r.get("event_name") or r.get("market_id") or "")
        if slug_resolved:
            event_url = f"https://polymarket.com/event/{slug_resolved}"
            link = ResolvedPolymarketLink(
                event_url=event_url, embed_url=None, link_type="canonical_event",
                source="db_cache", reason="slug_from_link_map",
                market_id=market_id_for_slug, token_id=r.get("token_id"),
                event_slug=slug_resolved, market_slug=slug_resolved, event_name=resolved_name,
            )
        else:
            fallback_url = f"https://polymarket.com/search?q={urllib.parse.quote(str(market_id_for_slug or ''))}"
            link = ResolvedPolymarketLink(
                event_url=fallback_url, embed_url=None, link_type="search_fallback",
                source="fallback", reason="no_slug_in_cache",
                market_id=market_id_for_slug, token_id=r.get("token_id"),
                event_slug=None, market_slug=None, event_name=None,
            )

    trades.append(
        TradeItem(
            tradeId=r["trade_id"],
            marketId=r["market_id"],
            eventName=str(resolved_name),
            eventUrl=link.event_url,
            linkType=link.link_type,
            linkSource=link.source,
            linkReason=link.reason,
            direction=r["direction"],
            confidence=_safe_float(r["confidence"]),
            openReason=r["open_reason"],
            entryPrice=_safe_float(r["entry_price"]),
            exitPrice=_safe_float(r["exit_price"]),
            positionSizeUsd=_safe_float(r["position_size_usd"]),
            estimatedEvUsd=_safe_float(r["estimated_ev_usd"]),
            realizedPnlUsd=_safe_float(r["realized_pnl_usd"]),
            unrealizedPnlUsd=_safe_float(r.get("unrealized_pnl_usd", 0.0)),
            status=trade_status,
            markPrice=_safe_float(r.get("mark_price")),
            updatedAt=str(r.get("updated_at") or r.get("closed_ts_utc")),
            closeCondition=r["close_condition"],
            openedAt=r["opened_ts_utc"],
            closedAt=r["closed_ts_utc"],
            source=r.get("source", "live"),
        )
    )

db.close()

# Time the Pydantic model_validate call
import time as t_module
t0 = t_module.monotonic()
for _ in range(5):
    response = RecommendationsResponse(trades=trades)
t1 = t_module.monotonic()
print(f'5x RecommendationsResponse(): {(t1-t0)*1000:.0f}ms avg={(t1-t0)*1000/5:.1f}ms per call')
print(f'trades={len(response.trades)}')
