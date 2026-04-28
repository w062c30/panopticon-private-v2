import sqlite3

conn = sqlite3.connect(':memory:')
conn.execute('''CREATE TABLE polymarket_link_map
                (token_id TEXT PRIMARY KEY COLLATE NOCASE,
                 event_slug TEXT)''')
conn.execute("INSERT INTO polymarket_link_map VALUES ('ABC123','test-slug')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('XYZ789','another-slug')")
conn.commit()

# Test 1: lowercase lookup finds UPPERCASE key
rows = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map '
    'WHERE token_id IN (?,?) COLLATE NOCASE',
    ('abc123', 'xyz999')
).fetchall()
print('Test 1 (lowercase IN, NOCASE):', rows)
# Expected: [('ABC123', 'test-slug')]

# Test 2: all three in one query
token_ids = ['abc123', 'xyz789', 'missing999']
placeholders = ','.join('?' * len(token_ids))
rows2 = conn.execute(
    f'SELECT token_id, COALESCE(event_slug, "") FROM polymarket_link_map '
    f'WHERE token_id IN ({placeholders}) COLLATE NOCASE',
    token_ids
).fetchall()
print('Test 2 (batch 3 IDs):', rows2)
# Expected: [('ABC123', 'test-slug'), ('XYZ789', 'another-slug')]

# Test 3: Verify COLLATE NOCASE works on PRIMARY KEY
rows3 = conn.execute(
    'SELECT token_id FROM polymarket_link_map WHERE token_id = ? COLLATE NOCASE',
    ('abc123',)
).fetchall()
print('Test 3 (PRIMARY KEY NOCASE =):', rows3)

conn.close()
print('\nAll tests passed!' if len(rows2) == 2 else '\nFAILED!')