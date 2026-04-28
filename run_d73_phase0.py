"""D73 Phase 0 Diagnostic — 15-minute observation window after D73a/b fix"""
import sqlite3
from datetime import datetime, timezone

DB = "data/panopticon.db"
now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
print(f"\n=== D73 Phase 0 Diagnostic === {now_utc} UTC")
print(f"DB: {DB}\n")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Q1: accepted=1
cur.execute("SELECT COUNT(*) FROM execution_records WHERE accepted=1")
q1 = cur.fetchone()[0] or 0
print(f"Q1 accepted=1: {q1}")

# Q2: gate_reason distribution
cur.execute("""
    SELECT gate_reason, COUNT(*) as cnt
    FROM execution_records
    WHERE created_ts_utc > datetime('now', '-15 minutes')
    GROUP BY gate_reason ORDER BY cnt DESC LIMIT 10
""")
print(f"\nQ2 gate_reason (last 15min):")
for row in cur.fetchall():
    print(f"  {row[0] or '(null)'}: {row[1]}")

# Q3: insider_score_snapshots last 15 min
cur.execute("""
    SELECT COUNT(*) FROM insider_score_snapshots
    WHERE ingest_ts_utc >= datetime('now', '-15 minutes')
""")
q3 = cur.fetchone()[0] or 0
print(f"\nQ3 insider_score_snapshots (last 15min): {q3}")

# Q4: wallet_observations T1
cur.execute("""
    SELECT COUNT(*) FROM wallet_observations
    WHERE market_id IN (
        SELECT token_id FROM polymarket_link_map WHERE market_tier='t1'
    )
""")
q4 = cur.fetchone()[0] or 0
print(f"\nQ4 wallet_observations T1: {q4}")

# Q5: discovered_entities
cur.execute("SELECT COUNT(*) FROM discovered_entities WHERE insider_score > 0")
q5 = cur.fetchone()[0] or 0
print(f"\nQ5 discovered_entities insider_score > 0: {q5}")

# Q6: execution_records total last 15 min
cur.execute("""
    SELECT COUNT(*) FROM execution_records
    WHERE created_ts_utc > datetime('now', '-15 minutes')
""")
q6 = cur.fetchone()[0] or 0
print(f"\nQ6 execution_records total (last 15min): {q6}")

# Q7: insider_score_snapshots all time
cur.execute("SELECT COUNT(*) FROM insider_score_snapshots")
q7 = cur.fetchone()[0] or 0
print(f"\nQ7 insider_score_snapshots all time: {q7}")

conn.close()
print("\n=== Diagnostic Complete ===")