import time, urllib.request, json, socket

# Test urllib with explicit Connection: close header
url = 'http://localhost:8001/api/recommendations?limit=20'

print("=== urllib with Connection: close ===")
for i in range(5):
    req = urllib.request.Request(url, headers={'Connection': 'close'})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=15.0) as resp:
        data = json.load(resp)
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms')

print()

# Also test with httpx Client() context manager (reuses connection)
print("=== httpx Client (with connection pool) ===")
import httpx
for i in range(5):
    t0 = time.monotonic()
    with httpx.Client(timeout=10.0) as client:
        r = client.get('http://localhost:8001/api/recommendations?limit=20')
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms')

print()

# Also test: httpx without connection pool (fresh connection each time)
print("=== httpx without connection pool (fresh) ===")
for i in range(5):
    t0 = time.monotonic()
    r = httpx.get(url, timeout=15.0)
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms')
