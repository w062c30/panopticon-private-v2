import time, sys, httpx

# Test raw httpx call (no API involvement)
t0 = time.monotonic()
with httpx.Client(timeout=15.0) as client:
    r = client.get('http://localhost:8001/api/recommendations?limit=20')
    t1 = time.monotonic()
    print(f'HTTP transport (no parse): {(t1-t0)*1000:.0f}ms status={r.status_code}')
    
    t2 = time.monotonic()
    data = r.json()
    t3 = time.monotonic()
    print(f'JSON parse: {(t3-t2)*1000:.0f}ms')
    
print(f'Total: {(t1-t0)*1000:.0f}ms')
