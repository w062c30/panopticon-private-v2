import time, urllib.request, json

url = 'http://localhost:8001/api/recommendations?limit=20'

# Test urllib.request (same as the backend uses for external APIs)
print("=== urllib.request (synchronous) ===")
for i in range(3):
    t0 = time.monotonic()
    with urllib.request.urlopen(url, timeout=15.0) as resp:
        data = json.load(resp)
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms trades={len(data)}')

print()

# Test httpx with explicit timeout
print("=== httpx (explicit timeout) ===")
import httpx
for i in range(3):
    t0 = time.monotonic()
    r = httpx.get(url, timeout=15.0)
    t1 = time.monotonic()
    data = r.json()
    t2 = time.monotonic()
    print(f'  Run {i+1}: fetch={(t1-t0)*1000:.0f}ms json={(t2-t1)*1000:.0f}ms total={(t2-t0)*1000:.0f}ms trades={len(data)}')
