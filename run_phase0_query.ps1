import sqlite3

conn = sqlite3.connect("data/panopticon.db")
conn.row_factory = sqlite3.Row

print("=== D71 Phase 0 Results ===\n")

# Q-A
q_a = conn.execute("SELECT count(*) FROM execution_records WHERE accepted=1").fetchone()[0]
print(f"Q-A: accepted=1 count: {q_a}")

# Q-B
q_b = conn.execute("SELECT count(*), source, market_tier FROM polymarket_link_map GROUP BY source, market_tier").fetchall()
print(f"\nQ-B: polymarket_link_map breakdown:")
for row in q_b:
    print(f"  count={row[0]} source={row[1]} tier={row[2]}")

# Q-C
q_c = conn.execute("""
    SELECT gate_reason, count(*) as n
    FROM execution_records
    GROUP BY gate_reason ORDER BY n DESC LIMIT 10
""").fetchall()
print(f"\nQ-C: gate_reason distribution (last 30min):")
for row in q_c:
    print(f"  gate_reason={row[0] or 'NULL'}: {row[1]}")

# Q-D
q_d = conn.execute("SELECT count(*) FROM wallet_activity WHERE usdc_size > 0").fetchone()[0]
print(f"\nQ-D: wallet_activity with usdc_size > 0: {q_d}")

# Extra: link_map t1 count
t1 = conn.execute("SELECT count(*) FROM polymarket_link_map WHERE market_tier='t1'").fetchone()[0]
print(f"\nExtra: link_map t1 rows: {t1}")

conn.close()
