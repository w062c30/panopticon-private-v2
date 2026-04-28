from panopticon_py.db import ShadowDB
db = ShadowDB()
c = db.conn

print("=== FULL SYSTEM STATUS ===\n")

# Kyle lambda
kyle = c.execute("SELECT COUNT(*) FROM kyle_lambda_samples").fetchone()[0]
kyle10 = c.execute("SELECT COUNT(*) FROM kyle_lambda_samples WHERE ts_utc > datetime('now', '-10 minutes')").fetchone()[0]
print("Kyle lambda: total={} new(last 10min)={}".format(kyle, kyle10))

# Whale alerts
wa = c.execute("SELECT COUNT(*) FROM whale_alerts").fetchone()[0]
wa10 = c.execute("SELECT COUNT(*) FROM whale_alerts WHERE ts_utc > datetime('now', '-10 minutes')").fetchone()[0]
print("Whale alerts: total={} new(last 10min)={}".format(wa, wa10))

# wallet_observations
wo = c.execute("SELECT COUNT(*) FROM wallet_observations").fetchone()[0]
wo10 = c.execute("SELECT COUNT(*) FROM wallet_observations WHERE ingest_ts_utc > datetime('now', '-10 minutes')").fetchone()[0]
print("wallet_observations: total={} new(last 10min)={}".format(wo, wo10))

# discovered_entities
de = c.execute("SELECT COUNT(*) FROM discovered_entities").fetchone()[0]
print("discovered_entities: total={}".format(de))

# execution_records
er_total = c.execute("SELECT COUNT(*) FROM execution_records").fetchone()[0]
er_acc = c.execute("SELECT COUNT(*) FROM execution_records WHERE accepted=1").fetchone()[0]
er_skip = c.execute("SELECT COUNT(*) FROM execution_records WHERE accepted=0").fetchone()[0]
er_reason = c.execute("SELECT reason, COUNT(*) as n FROM execution_records GROUP BY reason ORDER BY n DESC").fetchall()
print("\nexecution_records: total={} accepted={} rejected={}".format(er_total, er_acc, er_skip))
for r, n in er_reason:
    print("  reason={} n={}".format(str(r)[:60], n))

# Recent execution records (last 5)
print("\nRecent execution_records:")
rows = c.execute("SELECT execution_id, accepted, reason, created_ts_utc FROM execution_records ORDER BY rowid DESC LIMIT 5").fetchall()
for r in rows:
    print("  {} accepted={} reason={} ts={}".format(r[0][:16], r[1], str(r[2])[:40], r[3]))

# Check whale alerts source
print("\nWhale alerts breakdown:")
wa_recent = c.execute("SELECT wallet, size_usd, score, ts_utc FROM whale_alerts ORDER BY rowid DESC LIMIT 5").fetchall()
for r in wa_recent:
    print("  wallet={} size=${:.0f} score={} ts={}".format(r[0][:16], r[1], r[2], r[3]))

# Check MIN_CONSENSUS_SOURCES
from panopticon_py.signal_engine import MIN_CONSENSUS_SOURCES, INSIDER_SCORE_THRESHOLD, ENTROPY_LOOKBACK_SEC
print("\nSignal engine gate:")
print("  MIN_CONSENSUS_SOURCES={}".format(MIN_CONSENSUS_SOURCES))
print("  INSIDER_SCORE_THRESHOLD={}".format(INSIDER_SCORE_THRESHOLD))
print("  ENTROPY_LOOKBACK_SEC={}".format(ENTROPY_LOOKBACK_SEC))

# How many markets have 2+ insider sources in last 60s?
print("\nMarkets with 2+ insider sources in last 60s:")
try:
    insider_markets = c.execute("""
        SELECT market_id, COUNT(DISTINCT address) as n_sources
        FROM wallet_observations
        WHERE address != 'ws_unknown'
          AND address != '0x0000000000000000000000000000000000000000'
          AND ingest_ts_utc > datetime('now', '-60 seconds')
        GROUP BY market_id
        HAVING n_sources >= 2
        LIMIT 10
    """).fetchall()
    if insider_markets:
        for m in insider_markets:
            print("  market_id={} sources={}".format(m[0][:20], m[1]))
    else:
        print("  None found (no market has 2+ insider sources in last 60s)")
except Exception as e:
    print("  Error: " + str(e))
