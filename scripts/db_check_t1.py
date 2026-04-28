"""Check DB for T1 subscription health."""
import sqlite3
import os

db_path = os.path.join(os.path.dirname(__file__), "..", "data", "panopticon.db")
print(f"DB path: {db_path}")
print(f"Exists: {os.path.exists(db_path)}")

if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # kyle_lambda_samples schema
    c.execute("PRAGMA table_info(kyle_lambda_samples)")
    cols = [row[1] for row in c.fetchall()]
    print(f"kyle_lambda_samples columns: {cols}")

    # Count recent
    c.execute("SELECT COUNT(*) FROM kyle_lambda_samples")
    total = c.fetchone()[0]
    print(f"kyle_lambda_samples total rows: {total}")

    # pending_entropy_signals schema
    c.execute("PRAGMA table_info(pending_entropy_signals)")
    cols2 = [row[1] for row in c.fetchall()]
    print(f"pending_entropy_signals columns: {cols2}")

    # Show recent kyle samples
    if total > 0:
        c.execute(f"SELECT * FROM kyle_lambda_samples ORDER BY rowid DESC LIMIT 3")
        rows = c.fetchall()
        print("Recent kyle samples:")
        for r in rows:
            print(f"  {r}")

    conn.close()
else:
    print("DB not found")