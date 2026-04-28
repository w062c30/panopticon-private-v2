import sqlite3
conn = sqlite3.connect('data/panopticon.db')
cur = conn.cursor()

# Check realized_pnl_settlement schema
cur.execute("PRAGMA table_info('realized_pnl_settlement')")
print('realized_pnl_settlement columns:')
for row in cur.fetchall():
    print(f'  {row[1]}: {row[2]}')

# Check all rows in realized_pnl_settlement
cur.execute("SELECT COUNT(*) FROM realized_pnl_settlement")
print(f'\nrealized_pnl_settlement rows: {cur.fetchone()[0]}')

# Check entry/exit prices
cur.execute("SELECT entry_price, exit_price, realized_pnl_usd, estimated_ev_usd FROM realized_pnl_settlement LIMIT 10")
print('\nEntry/exit prices:')
for row in cur.fetchall():
    print(f'  entry={row[0]}, exit={row[1]}, realized={row[2]}, estimated={row[3]}')

# Check close_condition distribution
cur.execute("SELECT close_condition, COUNT(*) FROM realized_pnl_settlement GROUP BY close_condition")
print('\nClose condition:')
for row in cur.fetchall():
    print(f'  {row[0]}: {row[1]}')

# Check pnl stats
cur.execute("SELECT SUM(realized_pnl_usd), AVG(realized_pnl_usd), MIN(realized_pnl_usd), MAX(realized_pnl_usd) FROM realized_pnl_settlement")
row = cur.fetchone()
print(f'\nPnL stats: sum={row[0]}, avg={row[1]}, min={row[2]}, max={row[3]}')

# Check wallet_observations count
cur.execute("SELECT COUNT(*) FROM wallet_observations")
print(f'\nwallet_observations rows: {cur.fetchone()[0]}')

# Check entropy events
cur.execute("SELECT COUNT(*) FROM entropy_events")
print(f'entropy_events rows: {cur.fetchone()[0]}')

# Check clob_trades
cur.execute("SELECT COUNT(*) FROM clob_trades")
print(f'clob_trades rows: {cur.fetchone()[0]}')

conn.close()
