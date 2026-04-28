import time, subprocess

# Test: curl with Connection: close (fresh socket each time)
print("=== Test: curl with Connection: close ===")
for i in range(5):
    t0 = time.monotonic()
    r = subprocess.run(
        ['curl', '-s', '-H', 'Connection: close',
         'http://localhost:8001/api/recommendations?limit=20'],
        capture_output=True, text=True, timeout=15
    )
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms len={len(r.stdout)}')

print()

# Test: curl with Keep-Alive (connection reuse)
print("=== Test: curl with Keep-Alive ===")
for i in range(5):
    t0 = time.monotonic()
    r = subprocess.run(
        ['curl', '-s', '-H', 'Connection: keep-alive',
         'http://localhost:8001/api/recommendations?limit=20'],
        capture_output=True, text=True, timeout=15
    )
    t1 = time.monotonic()
    print(f'  Run {i+1}: {(t1-t0)*1000:.0f}ms len={len(r.stdout)}')

print()
print("If Keep-Alive runs 1x slow then 4x fast: uvicorn ASGI event loop blocking")
print("If Keep-Alive all fast: network/proxy overhead")
print("If Close all slow: connection overhead")
