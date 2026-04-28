import sqlite3

conn = sqlite3.connect(':memory:')
# Production schema: token_id is NOT primary key
conn.execute('''CREATE TABLE polymarket_link_map
                (market_id TEXT PRIMARY KEY,
                 token_id TEXT,
                 event_slug TEXT,
                 market_slug TEXT,
                 canonical_event_url TEXT,
                 source TEXT,
                 fetched_at TEXT)''')
conn.execute("INSERT INTO polymarket_link_map VALUES ('m1', 'ID1', 'slug-one', '', '', 'src', 'now')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('m2', 'id2', 'slug-two', '', '', 'src', 'now')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('m3', 'ID3', 'slug-three', '', '', 'src', 'now')")
conn.commit()

# Test 1: COLLATE NOCASE IN with token_id
rows1 = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map WHERE token_id IN (?,?) COLLATE NOCASE',
    ('ID1', 'id2')
).fetchall()
print('COLLATE NOCASE IN:', rows1)

# Test 2: LOWER() approach
rows2 = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map WHERE LOWER(token_id) IN (?,?)',
    ('id1', 'id2')
).fetchall()
print('LOWER() IN:', rows2)

# Test 3: resolve_slug equivalent
row = conn.execute(
    'SELECT event_slug FROM polymarket_link_map WHERE token_id = ? COLLATE NOCASE LIMIT 1',
    ('id1',)
).fetchone()
print('resolve_slug equiv id1:', row)
row2 = conn.execute(
    'SELECT event_slug FROM polymarket_link_map WHERE token_id = ? COLLATE NOCASE LIMIT 1',
    ('ID1',)
).fetchone()
print('resolve_slug equiv ID1:', row2)

conn.close()