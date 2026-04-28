import httpx, time

# Warm up
httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=5.0)
time.sleep(1)

# Time it
start = time.monotonic()
r = httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=5.0)
elapsed = time.monotonic() - start
print(f'Total elapsed: {elapsed:.3f}s')
print(f'Response: {len(r.json().get("trades", []))} trades')