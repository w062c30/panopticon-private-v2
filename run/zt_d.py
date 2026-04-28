import sqlite3

conn = sqlite3.connect("data/panopticon.db")
rows = [
    ("execution_records accepted=1",
     "SELECT COUNT(*) FROM execution_records WHERE accepted=1"),
    ("  with real entry (>0)",
     "SELECT COUNT(*) FROM execution_records WHERE accepted=1 AND avg_entry_price>0"),
    ("  with zero entry (legacy)",
     "SELECT COUNT(*) FROM execution_records WHERE accepted=1 AND avg_entry_price=0.0"),
    ("realized_pnl_settlement",
     "SELECT COUNT(*) FROM realized_pnl_settlement"),
    ("  with real exit price",
     "SELECT COUNT(*) FROM realized_pnl_settlement WHERE exit_price IS NOT NULL"),
    ("backup rows",
     "SELECT COUNT(*) FROM execution_records_pre_d66_backup"),
    ("realized_pnl backup rows",
     "SELECT COUNT(*) FROM realized_pnl_pre_d66_backup"),
]
for label, sql in rows:
    n = conn.execute(sql).fetchone()[0]
    print(f"{label}: {n}")
conn.close()
