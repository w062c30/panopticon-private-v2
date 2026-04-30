import sqlite3
from datetime import datetime, timezone

conn = sqlite3.connect('data/panopticon.db')
baseline_start = '2026-04-29T13:34:11Z'

tables_with_ts = {
    'wallet_observations': 'ingest_ts_utc',
    'insider_score_snapshots': 'ingest_ts_utc',
}

for table, ts_col in tables_with_ts.items():
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    since_baseline = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {ts_col} >= '{baseline_start}'"
    ).fetchone()[0]
    delta = total - 5867 if table == 'wallet_observations' else total - 3120
    print(f"{table}: total={total} (+{since_baseline - 5867 if table == 'wallet_observations' else since_baseline - 3120} since baseline)")

de = conn.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0]
de_scored = conn.execute("SELECT COUNT(*) FROM discovered_entities WHERE insider_score > 0").fetchone()[0]
print(f"discovered_entities: total={de}, with_score={de_scored}")

# execution_records uses created_ts_utc
er_total = conn.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
er_since = conn.execute(f"SELECT COUNT(*) FROM execution_records WHERE created_ts_utc >= '{baseline_start}'").fetchone()[0]
print(f"execution_records: total={er_total}, since_baseline={er_since}")

# wallet_market_positions
wmp = conn.execute("SELECT COUNT(*) FROM wallet_market_positions").fetchone()[0]
print(f"wallet_market_positions: total={wmp}")

conn.close()