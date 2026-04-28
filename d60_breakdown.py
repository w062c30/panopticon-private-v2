import sys; sys.path.insert(0, r'd:\Antigravity\Panopticon')
import time
from panopticon_py.db import ShadowDB
from panopticon_py.polymarket.live_trade_pnl_service import fetch_hybrid_trade_rows

db = ShadowDB('d:/Antigravity/Panopticon/data/panopticon.db')

# Time each component
t0 = time.monotonic()
db.bootstrap()
t1 = time.monotonic()
synced = db.sync_paper_trades_to_settlement()
t2 = time.monotonic()
rows = fetch_hybrid_trade_rows(db, limit=20, use_http_for_closed=False)
t3 = time.monotonic()
token_ids = [r.get("market_id") for r in rows if r.get("market_id")]
slug_map = db.batch_resolve_slugs(token_ids)
t4 = time.monotonic()
db.close()

print(f'bootstrap():    {(t1-t0)*1000:.1f}ms')
print(f'sync_paper():   {(t2-t1)*1000:.1f}ms (synced={synced})')
print(f'fetch_trades(): {(t3-t2)*1000:.1f}ms (rows={len(rows)})')
print(f'batch_slugs():  {(t4-t3)*1000:.1f}ms (ids={len(token_ids)})')
print(f'TOTAL:          {(t4-t0)*1000:.1f}ms')