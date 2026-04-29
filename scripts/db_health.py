import sqlite3

BASELINE = {
    'wallet_observations': 5867,
    'insider_score_snapshots': 3120,
    'wallet_market_positions': 1663,
    'execution_records': 24,
    'baseline_start': '2026-04-29T13:34:11Z',
}

conn = sqlite3.connect('data/panopticon.db')

# wallet_observations
n = conn.execute("SELECT COUNT(*) FROM wallet_observations").fetchone()[0]
recent = conn.execute("SELECT COUNT(*) FROM wallet_observations WHERE ingest_ts_utc >= datetime('now','-1 hour')").fetchone()[0]
delta = n - BASELINE['wallet_observations']
print(f"wallet_observations: total={n} (delta=+{delta} vs baseline), last_1h={recent}")

# discovered_entities
de = conn.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0]
de_scored = conn.execute("SELECT COUNT(*) FROM discovered_entities WHERE insider_score > 0").fetchone()[0]
print(f"discovered_entities: total={de}, with_score={de_scored}")

# insider_score_snapshots
n2 = conn.execute("SELECT COUNT(*) FROM insider_score_snapshots").fetchone()[0]
recent2 = conn.execute("SELECT COUNT(*) FROM insider_score_snapshots WHERE ingest_ts_utc >= datetime('now','-1 hour')").fetchone()[0]
delta2 = n2 - BASELINE['insider_score_snapshots']
print(f"insider_score_snapshots: total={n2} (delta=+{delta2} vs baseline), last_1h={recent2}")

# execution_records (uses created_ts_utc)
er = conn.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
recent_er = conn.execute("SELECT COUNT(*) FROM execution_records WHERE created_ts_utc >= datetime('now','-1 hour')").fetchone()[0]
delta_er = er - BASELINE['execution_records']
print(f"execution_records: total={er} (delta=+{delta_er} vs baseline), last_1h={recent_er}")

# wallet_market_positions
wmp = conn.execute("SELECT COUNT(*) FROM wallet_market_positions").fetchone()[0]
delta_wmp = wmp - BASELINE['wallet_market_positions']
print(f"wallet_market_positions: total={wmp} (delta=+{delta_wmp} vs baseline)")

conn.close()