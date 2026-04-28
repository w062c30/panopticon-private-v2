import httpx, time

for run in range(3):
    start = time.monotonic()
    r = httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=10.0)
    elapsed = time.monotonic() - start
    count = len(r.json().get('trades', []))
    print(f'Run {run+1}: {elapsed:.3f}s trades={count}')
    time.sleep(1)
