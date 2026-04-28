import sqlite3
conn = sqlite3.connect('data/panopticon.db')
cur = conn.cursor()

# Check execution_records with paper trades
cur.execute("SELECT COUNT(*) FROM execution_records WHERE mode='PAPER'")
print(f'execution_records PAPER rows: {cur.fetchone()[0]}')

# Check accepted distribution
cur.execute("SELECT accepted, COUNT(*), AVG(posterior), AVG(p_adj) FROM execution_records WHERE mode='PAPER' GROUP BY accepted")
print('\nAccepted distribution:')
for row in cur.fetchall():
    print(f'  accepted={row[0]}: count={row[1]}, avg_posterior={row[2]}, avg_p_adj={row[3]}')

# Check entry_price distribution
cur.execute("SELECT avg_entry_price, COUNT(*) FROM execution_records WHERE mode='PAPER' GROUP BY avg_entry_price ORDER BY COUNT(*) DESC LIMIT 10")
print('\navg_entry_price distribution:')
for row in cur.fetchall():
    print(f'  avg_entry_price={row[0]}: count={row[1]}')

# Check settlement_status distribution
cur.execute("SELECT settlement_status, COUNT(*) FROM execution_records WHERE mode='PAPER' GROUP BY settlement_status")
print('\nsettlement_status distribution:')
for row in cur.fetchall():
    print(f'  {row[0]}: {row[1]}')

# Check close_reason distribution
cur.execute("SELECT gate_reason, COUNT(*) FROM execution_records WHERE mode='PAPER' GROUP BY gate_reason")
print('\ngate_reason distribution:')
for row in cur.fetchall():
    print(f'  {row[0]}: {row[1]}')

# Sample some records
cur.execute("SELECT trade_id, accepted, avg_entry_price, posterior, p_adj, settlement_status, gate_reason FROM execution_records WHERE mode='PAPER' LIMIT 5")
print('\nSample records:')
for row in cur.fetchall():
    print(f'  {row}')

# Check polymarket_link_map
cur.execute('SELECT COUNT(*) FROM polymarket_link_map')
print(f'\npolymarket_link_map rows: {cur.fetchone()[0]}')

cur.execute('SELECT market_id, event_slug, market_slug FROM polymarket_link_map LIMIT 5')
print('polymarket_link_map samples:')
for row in cur.fetchall():
    print(f'  {row}')

conn.close()
