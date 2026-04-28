import sqlite3
conn = sqlite3.connect('data/panopticon.db')

# realized_pnl_settlement count
count = conn.execute('SELECT COUNT(*) FROM realized_pnl_settlement').fetchone()[0]
print(f'realized_pnl_settlement count: {count}')

# open_positions (check if exists)
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='open_positions'").fetchall()
print(f'open_positions exists: {bool(tables)}')

# Check indexes on realized_pnl_settlement
indexes = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='realized_pnl_settlement'").fetchall()
print()
print('=== Indexes on realized_pnl_settlement ===')
for idx in indexes:
    print(f'  {idx}')

# Verify fetch_trade_list query plan
plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM realized_pnl_settlement WHERE 1=1 ORDER BY closed_ts_utc DESC LIMIT 40").fetchall()
print()
print('=== Query plan ===')
for row in plan:
    print(f'  {row}')

# Check sync_paper_trades_to_settlement - check if it runs per-request
import re
with open('panopticon_py/api/routers/recommendations.py', encoding='utf-8') as f:
    content = f.read()
has_sync_call = 'sync_paper_trades_to_settlement' in content
print()
print(f'recommendations.py calls sync_paper_trades_to_settlement: {has_sync_call}')

# Check if fetch_trade_list uses the settlement table or execution_records
with open('panopticon_py/db.py', encoding='utf-8') as f:
    db_content = f.read()
m = re.search(r'def fetch_trade_list.*?return', db_content, re.DOTALL)
if m:
    print()
    print('fetch_trade_list snippet:')
    snippet = m.group(0)[:300]
    print(snippet[:300])

conn.close()
