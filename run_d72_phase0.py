"""D72 Phase 0 Diagnostic — 5-minute data collection after D72a fix"""
import sqlite3
from datetime import datetime, timezone

DB = "data/panopticon.db"
now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
print(f"\n=== D72 Phase 0 Diagnostic === {now_utc} UTC")
print(f"DB: {DB}\n")

conn = sqlite3.connect(DB)
cur = conn.cursor()

# Q-A: accepted=1 count
cur.execute("SELECT COUNT(*) FROM execution_records WHERE accepted = 1")
accepted_1 = cur.fetchone()[0] or 0
print(f"[Q-A] accepted=1: {accepted_1}")

# Q-B: polymarket_link_map breakdown
cur.execute("""
    SELECT source, market_tier, COUNT(*) as cnt
    FROM polymarket_link_map
    GROUP BY source, market_tier
    ORDER BY cnt DESC
""")
print(f"\n[Q-B] polymarket_link_map breakdown:")
for row in cur.fetchall():
    print(f"  source={row[0]} tier={row[1] or 'None'} count={row[2]}")

# Q-C: gate_reason distribution (last 30 min)
cur.execute("""
    SELECT gate_reason, COUNT(*) as cnt
    FROM execution_records
    WHERE created_ts_utc > datetime('now', '-30 minutes')
    GROUP BY gate_reason
    ORDER BY cnt DESC
    LIMIT 10
""")
print(f"\n[Q-C] gate_reason distribution (last 30 min):")
for row in cur.fetchall():
    print(f"  {row[0] or '(null)'}: {row[1]}")

# Q-D: wallet_activity
cur.execute("SELECT COUNT(*) FROM wallet_activity WHERE usdc_size > 0")
wa_count = cur.fetchone()[0] or 0
print(f"\n[Q-D] wallet_activity (usdc_size > 0): {wa_count}")

# Q-E: wallet_observations T1
cur.execute("""
    SELECT COUNT(*)
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
""")
wo_t1 = cur.fetchone()[0] or 0
print(f"\n[Q-E] wallet_observations T1 (tier=t1 from link_map): {wo_t1}")

# Q-F: wallet_observations total
cur.execute("SELECT COUNT(*) FROM wallet_observations WHERE obs_type = 'clob_trade'")
wo_total = cur.fetchone()[0] or 0
print(f"[Q-F] wallet_observations total (clob_trade): {wo_total}")

# Q-G: discovered_entities with insider_score for T1
cur.execute("""
    SELECT COUNT(*)
    FROM discovered_entities
    WHERE insider_score IS NOT NULL AND insider_score > 0
""")
de_count = cur.fetchone()[0] or 0
print(f"\n[Q-G] discovered_entities with insider_score > 0: {de_count}")

# Q-H: series_members t1 count
cur.execute("SELECT COUNT(*) FROM series_members WHERE market_tier = 't1'")
sm_t1 = cur.fetchone()[0] or 0
print(f"[Q-H] series_members t1: {sm_t1}")

# Q-I: insider_score_snapshots recent (last 30 min)
cur.execute("""
    SELECT COUNT(*)
    FROM insider_score_snapshots
    WHERE ingest_ts_utc > datetime('now', '-30 minutes')
""")
iss_count = cur.fetchone()[0] or 0
print(f"\n[Q-I] insider_score_snapshots (last 30 min): {iss_count}")

# Q-J: execution_records total (last 30 min)
cur.execute("""
    SELECT COUNT(*)
    FROM execution_records
    WHERE created_ts_utc > datetime('now', '-30 minutes')
""")
er_count = cur.fetchone()[0] or 0
print(f"[Q-J] execution_records total (last 30 min): {er_count}")

# Q-K: distinct wallets in wallet_observations with T1 market_id
cur.execute("""
    SELECT COUNT(DISTINCT wo.address)
    FROM wallet_observations wo
    JOIN polymarket_link_map pm ON pm.token_id = wo.market_id
    WHERE pm.market_tier = 't1' AND wo.obs_type = 'clob_trade'
""")
t1_wallets = cur.fetchone()[0] or 0
print(f"\n[Q-K] distinct wallets in wallet_observations with T1 market_id: {t1_wallets}")

conn.close()
print("\n=== Diagnostic Complete ===")
