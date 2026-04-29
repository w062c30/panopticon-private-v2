import sqlite3
conn = sqlite3.connect('data/panopticon.db')
tables = ['wallet_observations', 'discovered_entities', 'insider_score_snapshots', 'execution_records']
for t in tables:
    n = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    try:
        recent = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE ingest_ts_utc >= datetime('now','-1 hour')").fetchone()[0]
    except:
        try:
            recent = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE created_ts_utc >= datetime('now','-1 hour')").fetchone()[0]
        except:
            recent = 'N/A'
    print(f'{t}: total={n}, last_1h={recent}')
conn.close()