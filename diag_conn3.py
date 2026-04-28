import time, http.client, json

# Test http.client persistent connection - check both endpoints
conn = http.client.HTTPConnection('localhost', 8001, timeout=10)

print("=== http.client persistent (no close) ===")
endpoints = [
    '/api/versions',
    '/api/recommendations?limit=20',
    '/api/performance?period=7d',
    '/api/recommendations?limit=20',
    '/api/versions',
    '/api/recommendations?limit=20',
]

for ep in endpoints:
    t0 = time.monotonic()
    conn.request('GET', ep)
    resp = conn.getresponse()
    data = resp.read()
    t1 = time.monotonic()
    print(f'  {ep}: {(t1-t0)*1000:.0f}ms len={len(data)}')

conn.close()
