import sqlite3, time

conn = sqlite3.connect('data/panopticon.db')

# Check realized_pnl_settlement row count
t0 = time.monotonic()
count = conn.execute('SELECT COUNT(*) FROM realized_pnl_settlement').fetchone()[0]
t1 = time.monotonic()
print(f'realized_pnl_settlement count: {count} (query took {(t1-t0)*1000:.1f}ms)')

# Check if there's an index on closed_ts_utc
indexes = conn.execute(
    "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='realized_pnl_settlement'"
).fetchall()
print()
print('=== INDEXES ON realized_pnl_settlement ===')
for idx in indexes:
    print(f'  {idx}')

# EXPLAIN QUERY PLAN for fetch_trade_list query
print()
print('=== EXPLAIN QUERY PLAN ===')
plan = conn.execute(
    "EXPLAIN QUERY PLAN SELECT * FROM realized_pnl_settlement WHERE 1=1 ORDER BY closed_ts_utc DESC LIMIT 40"
).fetchall()
for row in plan:
    print(f'  {row}')

# Time the raw query
t2 = time.monotonic()
rows = conn.execute(
    "SELECT * FROM realized_pnl_settlement ORDER BY closed_ts_utc DESC LIMIT 40"
).fetchall()
t3 = time.monotonic()
print()
print(f'Raw SELECT (no WHERE): {(t3-t2)*1000:.1f}ms, rows={len(rows)}')

conn.close()
