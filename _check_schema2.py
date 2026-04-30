import sqlite3
conn = sqlite3.connect("data/panopticon.db")
cols = [r[1] for r in conn.execute("PRAGMA table_info(discovered_entities)").fetchall()]
print("discovered_entities columns:", cols)
conn.close()