import sqlite3
conn = sqlite3.connect("data/panopticon.db")
deleted = conn.execute(
    "DELETE FROM realized_pnl_settlement WHERE market_id LIKE 'demo-market-%'"
).rowcount
conn.commit()
print("Deleted demo rows:", deleted)
remaining = conn.execute("SELECT COUNT(*) FROM realized_pnl_settlement").fetchone()[0]
print("Remaining realized_pnl_settlement:", remaining)
conn.close()
