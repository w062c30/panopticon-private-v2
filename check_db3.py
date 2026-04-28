import sqlite3
conn = sqlite3.connect('data/panopticon.db')
cur = conn.cursor()

# Check how many trades in execution_records vs realized_pnl_settlement
cur.execute("SELECT COUNT(*) FROM execution_records WHERE mode='PAPER'")
exec_count = cur.fetchone()[0]
print(f'execution_records PAPER rows: {exec_count}')

cur.execute("SELECT COUNT(*) FROM realized_pnl_settlement")
settle_count = cur.fetchone()[0]
print(f'realized_pnl_settlement rows: {settle_count}')

# Check what percentage of execution_records accepted=1
cur.execute("SELECT accepted, COUNT(*) FROM execution_records WHERE mode='PAPER' GROUP BY accepted")
print('\nAccepted distribution:')
for row in cur.fetchall():
    print(f'  accepted={row[0]}: {row[1]}')

# Check wallet_observations breakdown by obs_type
cur.execute("SELECT obs_type, COUNT(*) FROM wallet_observations GROUP BY obs_type")
print('\nwallet_observations by obs_type:')
for row in cur.fetchall():
    print(f'  {row[0]}: {row[1]}')

# Check recent wallet_observations
cur.execute("SELECT obs_type, address, market_id, ingest_ts_utc FROM wallet_observations ORDER BY ingest_ts_utc DESC LIMIT 5")
print('\nRecent wallet_observations:')
for row in cur.fetchall():
    print(f'  {row}')

# Check clob_trades
cur.execute("SELECT COUNT(*) FROM clob_trades")
print(f'\nclob_trades rows: {cur.fetchone()[0]}')

# Check execution_records recent entries
cur.execute("SELECT accepted, gate_reason, avg_entry_price, posterior FROM execution_records WHERE mode='PAPER' ORDER BY created_ts_utc DESC LIMIT 5")
print('\nRecent execution_records:')
for row in cur.fetchall():
    print(f'  {row}')

conn.close()
