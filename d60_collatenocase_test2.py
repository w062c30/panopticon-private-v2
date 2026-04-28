import sqlite3
conn = sqlite3.connect(':memory:')
conn.execute('CREATE TABLE polymarket_link_map (token_id TEXT PRIMARY KEY, event_slug TEXT)')
conn.execute("INSERT INTO polymarket_link_map VALUES ('TOKEN_A', 'alpha-slg')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('token_b', 'beta-slg')")
conn.commit()

# Test 1: COLLATE NOCASE with IN clause (like batch_resolve_slugs originally tried)
rows1 = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map WHERE token_id IN (?,?) COLLATE NOCASE',
    ('TOKEN_A', 'token_b')
).fetchall()
print('Test 1 - COLLATE NOCASE IN (no placeholder):', rows1)
# Expected (broken): empty or wrong

# Test 2: COLLATE NOCASE on column with IN clause
rows2 = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map WHERE token_id COLLATE NOCASE IN (?,?)',
    ('TOKEN_A', 'token_b')
).fetchall()
print('Test 2 - COLLATE NOCASE column IN (no placeholder):', rows2)
# Expected (broken): empty or wrong

# Test 3: LOWER() on both sides
rows3 = conn.execute(
    'SELECT token_id, event_slug FROM polymarket_link_map WHERE LOWER(token_id) IN (?,?)',
    ('token_a', 'token_b')
).fetchall()
print('Test 3 - LOWER() IN:', rows3)

conn.close()
print('\nCorrect approach: LOWER() on both sides')