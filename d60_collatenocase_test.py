import sqlite3
conn = sqlite3.connect(':memory:')
conn.execute('CREATE TABLE polymarket_link_map (token_id TEXT PRIMARY KEY, event_slug TEXT)')
conn.execute("INSERT INTO polymarket_link_map VALUES ('TOKEN_A', 'alpha-slg')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('token_b', 'beta-slg')")
conn.commit()

# Test: does COLLATE NOCASE work for single-row lookup?
row = conn.execute(
    'SELECT event_slug FROM polymarket_link_map WHERE token_id = ? COLLATE NOCASE LIMIT 1',
    ('TOKEN_A',)
).fetchone()
print('Single row lookup TOKEN_A:', row)
row2 = conn.execute(
    'SELECT event_slug FROM polymarket_link_map WHERE token_id = ? COLLATE NOCASE LIMIT 1',
    ('token_a',)
).fetchone()
print('Single row lookup token_a:', row2)
conn.close()
print('PASS!' if row and row2 else 'FAIL!')