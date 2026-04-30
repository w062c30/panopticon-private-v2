import sqlite3
db = 'd:/Antigravity/Panopticon/data/panopticon.db'
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM order_reconstructions')
total = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM order_reconstructions WHERE is_complete=0')
open_count = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM order_reconstructions WHERE is_complete=1')
closed_count = cur.fetchone()[0]
print(f"order_reconstructions: {total} total, {open_count} open, {closed_count} closed")
conn.close()
