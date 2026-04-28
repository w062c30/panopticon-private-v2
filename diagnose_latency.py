import time, sys
sys.path.insert(0, '.')
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows
from panopticon_py.db import ShadowDB

db = ShadowDB()
db.bootstrap()

# Time fetch_hybrid_trade_rows with use_http_for_closed=False
t0 = time.monotonic()
rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)
t1 = time.monotonic()
print(f'fetch_hybrid_trade_rows (use_http=False): {(t1-t0)*1000:.0f}ms, rows={len(rows)}')

# Count open positions
open_pos = db.fetch_open_positions()
print(f'Open positions: {len(open_pos)}')
for pos in open_pos:
    mid = pos.get('market_id', 'N/A')
    print(f'  market_id={mid}, notional={pos.get("signed_notional_usd",0)}')

# Time fetch_hybrid_trade_rows with use_http_for_closed=True
t2 = time.monotonic()
rows2 = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=True)
t3 = time.monotonic()
print(f'fetch_hybrid_trade_rows (use_http=True):  {(t3-t2)*1000:.0f}ms, rows={len(rows2)}')

# Time the _latest_market_prices call path
from panopticon_py.polymarket.live_trade_pnl_service import _latest_market_prices
if open_pos:
    token_ids = [str(p.get('market_id')) for p in open_pos]
    t4 = time.monotonic()
    prices = _latest_market_prices(token_ids)
    t5 = time.monotonic()
    print(f'_latest_market_prices ({len(token_ids)} tokens): {(t5-t4)*1000:.0f}ms, prices={len(prices)}')

db.close()
