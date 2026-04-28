from panopticon_py.db import ShadowDB
db = ShadowDB()
c = db.conn

# Check current whale_alerts schema
cols = [r[1] for r in c.execute("PRAGMA table_info(whale_alerts)").fetchall()]
print("Current whale_alerts columns:", cols)

# Add missing columns
missing = ["book_depth_ask_usd", "book_depth_bid_usd", "spread"]
for col in missing:
    if col not in cols:
        try:
            c.execute("ALTER TABLE whale_alerts ADD COLUMN " + col + " REAL DEFAULT 0.0")
            print("Added column: " + col)
        except Exception as e:
            print("Error adding " + col + ": " + str(e))

# Verify
cols2 = [r[1] for r in c.execute("PRAGMA table_info(whale_alerts)").fetchall()]
print("Updated whale_alerts columns:", cols2)
