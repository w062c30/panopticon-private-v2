import httpx, time
start = time.monotonic()
r = httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=10.0)
elapsed = time.monotonic() - start
trades = r.json().get('trades', [])
print(f'BASELINE latency: {elapsed:.3f}s trades={len(trades)}')
for t in trades[:2]:
    print(f'  status={t.get("status")} link={t.get("linkType")} src={t.get("linkSource")}')