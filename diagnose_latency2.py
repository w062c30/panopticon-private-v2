import time, sys, datetime
sys.path.insert(0, '.')
import urllib.parse

from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.polymarket.link_resolver import resolve_polymarket_link, ResolvedPolymarketLink

t_start = time.monotonic()

# 1. ShadowDB instantiation
t0 = time.monotonic()
db = ShadowDB()
t1 = time.monotonic()
print(f'[T1] ShadowDB(): {(t1-t0)*1000:.0f}ms')

# 2. bootstrap
t2 = time.monotonic()
db.bootstrap()
t3 = time.monotonic()
print(f'[T2] bootstrap(): {(t3-t2)*1000:.0f}ms')

# 3. sync_paper_trades_to_settlement
t4 = time.monotonic()
synced = db.sync_paper_trades_to_settlement()
t5 = time.monotonic()
print(f'[T3] sync_paper_trades (synced={synced}): {(t5-t4)*1000:.0f}ms')

# 4. fetch_hybrid_trade_rows (use_http=False, as in the endpoint)
t6 = time.monotonic()
rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)
t7 = time.monotonic()
print(f'[T4] fetch_hybrid_trade_rows: {(t7-t6)*1000:.0f}ms, rows={len(rows)}')

# 5. batch_resolve_slugs
token_ids = [r.get("market_id") for r in rows if r.get("market_id")]
t8 = time.monotonic()
slug_map = db.batch_resolve_slugs(token_ids)
t9 = time.monotonic()
print(f'[T5] batch_resolve_slugs ({len(token_ids)} ids): {(t9-t8)*1000:.0f}ms')

# 6. TradeItem loop
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

    trades.append({
        "tradeId": r["trade_id"],
        "marketId": r["market_id"],
        "eventName": str(resolved_name),
        "eventUrl": link.event_url,
        "direction": r["direction"],
        "confidence": _safe_float(r["confidence"]),
        "status": trade_status,
    })

t10 = time.monotonic()
print(f'[T6] TradeItem loop: {(t10-t9)*1000:.0f}ms, trades={len(trades)}')

# 7. db.close()
t11 = time.monotonic()
db.close()
t12 = time.monotonic()
print(f'[T7] db.close(): {(t12-t11)*1000:.0f}ms')

t_end = time.monotonic()
print(f'')
print(f'TOTAL (outside FastAPI): {(t_end-t_start)*1000:.0f}ms')
