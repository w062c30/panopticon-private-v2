import sqlite3
conn = sqlite3.connect('data/panopticon.db')
rows = conn.execute("""
    SELECT ingest_ts_utc, address, market_id, score
    FROM insider_score_snapshots
    ORDER BY ingest_ts_utc DESC
    LIMIT 10
""").fetchall()
for r in rows:
    print(r)
conn.close()