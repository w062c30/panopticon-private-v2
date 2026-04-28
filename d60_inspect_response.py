import httpx, time

# Instrument the recommendations endpoint to find where time is spent
r = httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=10.0)
data = r.json()
trades = data.get('trades', [])
print(f'Status={r.status_code} trades={len(trades)}')

# Sample response
for t in trades[:3]:
    print(f'  status={t.get("status")} link_src={t.get("linkSource")} conf={t.get("confidence")}')

# Count sources
from collections import Counter
sources = Counter(t.get('linkSource') for t in trades)
print(f'\nLink sources: {dict(sources)}')