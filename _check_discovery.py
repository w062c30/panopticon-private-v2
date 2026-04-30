import sqlite3
conn = sqlite3.connect("data/panopticon.db")

# Distribution of discovery_source and insider_score in discovered_entities
print("=== discovered_entities breakdown ===")
rows = conn.execute("""
    SELECT discovery_source, COUNT(*), MIN(insider_score), MAX(insider_score), AVG(insider_score)
    FROM discovered_entities
    GROUP BY discovery_source
""").fetchall()
for r in rows:
    print(f"  source={r[0]} count={r[1]} min={r[2]:.3f} max={r[3]:.3f} avg={r[4]:.3f}")

print("\n=== discovered_entities with insider_score > 0 ===")
rows2 = conn.execute("SELECT entity_id, insider_score, discovery_source FROM discovered_entities WHERE insider_score > 0").fetchall()
print(f"  count={len(rows2)}")
for r in rows2[:5]:
    print(f"  {r[0][:12]}... score={r[1]:.3f} source={r[2]}")

print("\n=== discovered_entities total ===")
total = conn.execute("SELECT COUNT(*), AVG(insider_score), MIN(insider_score), MAX(insider_score) FROM discovered_entities").fetchone()
print(f"  total={total[0]} avg={total[1]:.3f} min={total[2]:.3f} max={total[3]:.3f}")

conn.close()