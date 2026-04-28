import sqlite3

conn = sqlite3.connect("data/panopticon.db")

# Check execution_records schema
print("execution_records columns:")
for row in conn.execute("PRAGMA table_info(execution_records)"):
    print(f"  {row[1]}: {row[2]}")

# Check recent execution_records
recent = conn.execute("SELECT count(*) FROM execution_records WHERE created_ts_utc > datetime('now', '-30 minutes')").fetchone()[0]
print(f"\nexecution_records (last 30min): {recent}")

# wallet_activity growth
wa_total = conn.execute("SELECT count(*) FROM wallet_activity").fetchone()[0]
wa_with_usd = conn.execute("SELECT count(*) FROM wallet_activity WHERE usdc_size > 0").fetchone()[0]
print(f"wallet_activity total: {wa_total}, with usdc_size > 0: {wa_with_usd}")

# token_ids from btc5m_resolver
resolver_tokens = [row[2] for row in conn.execute("SELECT slug, condition_id, token_id FROM polymarket_link_map WHERE source='btc5m_resolver'")]
print(f"\nbtc5m_resolver token_ids ({len(resolver_tokens)}):")
for t in resolver_tokens:
    print(f"  {str(t)[:30]}...")

# Check if these tokens appear in wallet_observations
for tid in resolver_tokens[:2]:
    obs_count = conn.execute("SELECT count(*) FROM wallet_observations WHERE market_id=?", (tid,)).fetchone()[0]
    print(f"  wallet_observations for token {str(tid)[:20]}...: {obs_count}")

conn.close()
