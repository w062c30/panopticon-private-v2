import time, sys, datetime
sys.path.insert(0, '.')

# Test: simulate the actual API call path
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.link_resolver import resolve_polymarket_link, ResolvedPolymarketLink
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

# Time each step with actual datetime.now() as done in the endpoint
def run_endpoint():
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
                        event_url=event_url,
                        embed_url=None,
                        link_type="canonical_event",
                        source="db_cache",
                        reason="slug_from_link_map",
                        market_id=market_id_for_slug,
                        token_id=r.get("token_id"),
                        event_slug=slug_resolved,
                        market_slug=slug_resolved,
                        event_name=resolved_name,
                    )
                else:
                    fallback_url = f"https://polymarket.com/search?q={urllib.parse.quote(str(market_id_for_slug or ''))}"
                    link = ResolvedPolymarketLink(
                        event_url=fallback_url,
                        embed_url=None,
                        link_type="search_fallback",
                        source="fallback",
                        reason="no_slug_in_cache",
                        market_id=market_id_for_slug,
                        token_id=r.get("token_id"),
                        event_slug=None,
                        market_slug=None,
                        event_name=None,
                    )
            trades.append({
                "tradeId": r["trade_id"],
                "marketId": r["market_id"],
                "eventName": str(resolved_name),
                "eventUrl": link.event_url,
                "direction": r["direction"],
                "confidence": _safe_float(r["confidence"]),
                "status": trade_status,
            })
        t6 = datetime.datetime.now()
        db.close()
        t7 = datetime.datetime.now()
        
        print(f'bootstrap={(t1-t0).total_seconds()*1000:.0f}ms')
        print(f'sync={(t2-t1).total_seconds()*1000:.0f}ms')
        print(f'fetch_trades={(t3-t2).total_seconds()*1000:.0f}ms')
        print(f'batch_slugs={(t4-t3).total_seconds()*1000:.0f}ms')
        print(f'loop={(t5-t4).total_seconds()*1000:.0f}ms')
        print(f'close={(t6-t5).total_seconds()*1000:.0f}ms')
        print(f'TOTAL={(t6-t0).total_seconds()*1000:.0f}ms trades={len(trades)}')
    finally:
        db.close()

# Run 3 times to check for variance
for i in range(3):
    print(f'=== Run {i+1} ===')
    run_endpoint()
    print()
