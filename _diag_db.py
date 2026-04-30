import os, datetime, sqlite3
db_path = 'd:/Antigravity/Panopticon/panopticon_py/data/panopticon.db'
stat = os.stat(db_path)
dt = datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc)
print(f'DB mtime: {dt} (UTC)')
print(f'DB size: {stat.st_size:,} bytes')

conn = sqlite3.connect(db_path)
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print(f'Tables ({len(tables)}):')
for r in tables:
    c = conn.execute(f'SELECT COUNT(*) FROM "{r[0]}"').fetchone()[0]
    print(f'  {r[0]}: {c}')
