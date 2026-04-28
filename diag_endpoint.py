import time, sys
sys.path.insert(0, '.')
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.link_resolver import resolve_polymarket_link, ResolvedPolymarketLink
from panopticon_py.api.schemas import RecommendationsResponse, TradeItem
import urllib.parse
import datetime

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

def time_endpoint():
    t0 = datetime.datetime.now()
    db = ShadowDB()
    try:
        t1 = datetime.datetime.now()
        db.bootstrap()
        t2 = datetime.datetime.now()
        synced = db.sync_paper_trades_to_settlement()
        if synced > 0:
            pass
        t3 = datetime.datetime.now()
        rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)
        t4 = datetime.datetime.now()
        token_ids = [r.get("market_id") for r in rows if r.get("market_id")]
        slug_map = db.batch_resolve_slugs(token_ids)
        t5 = datetime.datetime.now()
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
        t6 = datetime.datetime.now()
        response = RecommendationsResponse(trades=trades)
        t7 = datetime.datetime.now()
        db.close()
        t8 = datetime.datetime.now()
        
        print(f'[D61] breakdown:')
        print(f'  bootstrap={(t1-t0).total_seconds()*1000:.0f}ms')
        print(f'  sync={(t2-t1).total_seconds()*1000:.0f}ms')
        print(f'  fetch_trades={(t3-t2).total_seconds()*1000:.0f}ms')
        print(f'  batch_slugs={(t4-t3).total_seconds()*1000:.0f}ms')
        print(f'  loop+model={(t6-t4).total_seconds()*1000:.0f}ms')
        print(f'  model_validate={(t7-t6).total_seconds()*1000:.0f}ms')
        print(f'  close={(t8-t7).total_seconds()*1000:.0f}ms')
        print(f'  TOTAL={(t8-t0).total_seconds()*1000:.0f}ms trades={len(trades)}')
        return response
    finally:
        db.close()

# Run 3 times
for i in range(3):
    time_endpoint()
    print()
