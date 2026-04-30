import sqlite3
conn = sqlite3.connect("data/panopticon.db")

# Check discovered_entities schema
cols = [r[1] for r in conn.execute("PRAGMA table_info(discovered_entities)").fetchall()]
print("discovered_entities columns:", cols)

# Check what columns whale_scanner is trying to use
print("\n=== whale_scanner _upsert_whale_wallet_as_entity INSERT columns ===")
print("  whale_scanner tries: entity_id, insider_score, source, last_seen_market, updated_ts_utc, trust_score, primary_tag, sample_size, last_updated_at")
print("  actual columns:    ", cols)

# Check last_seen_market
print("\nHas last_seen_market:", "last_seen_market" in cols)
print("Has source:", "source" in cols)
print("Has updated_ts_utc:", "updated_ts_utc" in cols)

# How whale_scanner inserts vs upsert_discovered_entity inserts
print("\n=== upsert_discovered_entity INSERT columns ===")
print("  INSERT INTO discovered_entities (entity_id, trust_score, primary_tag, sample_size, last_updated_at)")

conn.close()