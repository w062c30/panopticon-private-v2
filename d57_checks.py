import sqlite3, httpx, time
from collections import Counter

# D57c Step 1: DB-based data quality (no API timeout)
conn = sqlite3.connect('d:/Antigravity/Panopticon/data/panopticon.db')
rows = conn.execute("""
    SELECT trade_id, market_id, event_name, confidence, entry_price,
           exit_price, position_size_usd, realized_pnl_usd, status, source,
           created_ts_utc, updated_at
    FROM execution_records
    WHERE accepted = 1
    ORDER BY created_ts_utc DESC
    LIMIT 10
""").fetchall()
conn.close()

print(f"DB accepted trades: {len(rows)}")
for r in rows:
    issues = []
    trade_id, market_id, event_name, confidence, entry_price = r[:5]
    realized_pnl, status, source = r[7:10]
    conf_type = type(confidence).__name__
    conf_val = float(confidence) if confidence is not None else None
    conf_disp = f"{conf_val*100:.1f}%" if isinstance(conf_val, float) else f"BAD({confidence})"
    pnl_disp = f"${float(realized_pnl):.4f}" if realized_pnl is not None else "null"
    icon = "✅" if not issues else "❌"
    name = str(event_name or market_id or "")[:45]
    print(f"  {icon} [{str(source):15}][{str(status):6}] conf={conf_disp} PnL={pnl_disp} | {name}")
    if issues:
        for iss in issues:
            print(f"     ⚠️  {iss}")

# D57c Step 2: Try API with small limit
print("\n--- API check ---")
try:
    start = time.monotonic()
    r = httpx.get("http://localhost:8001/api/recommendations?limit=3", timeout=60.0)
    elapsed = time.monotonic() - start
    data = r.json()
    trades = data if isinstance(data, list) else data.get("trades", [])
    print(f"  API: {len(trades)} trades in {elapsed:.1f}s")
    for t in trades[:3]:
        c = t.get("confidence")
        p = t.get("realizedPnlUsd")
        src = t.get("source", "?")
        st = t.get("status", "?")
        nm = str(t.get("eventName",""))[:45]
        c_ok = isinstance(c, (int,float))
        print(f"  {'✅' if c_ok else '❌'} [{src}][{st}] conf={c*100:.1f}% PnL={p} | {nm}")
except Exception as e:
    print(f"  API ERROR: {type(e).__name__}: {e}")

# D57c Step 3: Source/status distribution from DB
print("\n--- Distribution ---")
conn2 = sqlite3.connect('d:/Antigravity/Panopticon/data/panopticon.db')
all_rows = conn2.execute("SELECT source, status, COUNT(*) FROM execution_records WHERE accepted=1 GROUP BY source, status").fetchall()
conn2.close()
for source, status, count in all_rows:
    print(f"  {source}/{status}: {count}")
