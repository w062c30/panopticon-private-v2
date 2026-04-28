import sqlite3
conn = sqlite3.connect('data/panopticon.db')
info = conn.execute('PRAGMA table_info(discovered_entities)').fetchall()
print('=== discovered_entities schema ===')
for row in info:
    print(f'  {row}')
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='discovered_entities'").fetchall()
print()
print('discovered_entities exists:', bool(tables))
conn.close()
