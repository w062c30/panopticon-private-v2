import sqlite3

conn = sqlite3.connect(':memory:')
conn.execute('''CREATE TABLE polymarket_link_map
                (token_id TEXT PRIMARY KEY,
                 event_slug TEXT)''')
conn.execute("INSERT INTO polymarket_link_map VALUES ('ABC123','test-slug')")
conn.execute("INSERT INTO polymarket_link_map VALUES ('XYZ789','another-slug')")
conn.commit()

# Test LOWER() approach
def batch_resolve(token_ids):
    lowered_ids = [t.lower() for t in token_ids]
    placeholders = ",".join("?" * len(token_ids))
    rows = conn.execute(
        f"""SELECT m.token_id, COALESCE(m.event_slug, '')
            FROM polymarket_link_map m
            WHERE LOWER(m.token_id) IN ({placeholders})""",
        lowered_ids,
    ).fetchall()
    result = {tid: "" for tid in token_ids}
    for tok_id, slug in rows:
        for tid in token_ids:
            if tid.lower() == tok_id.lower():
                result[tid] = slug
    return result

r = batch_resolve(['abc123', 'XYZ789', 'missing'])
print('Result:', r)
expected = {'abc123': 'test-slug', 'XYZ789': 'another-slug', 'missing': ''}
print('Expected:', expected)
print('PASS!' if r == expected else f'MISMATCH!')

# Performance: 50 IDs
import time
ids = [f'ID_{i}' for i in range(50)]
start = time.monotonic()
for _ in range(1000):
    batch_resolve(ids)
elapsed = time.monotonic() - start
print(f'\nBatch 50 IDs x 1000 iterations: {elapsed*1000:.1f}ms ({elapsed*1000/1000:.4f}ms per call)')