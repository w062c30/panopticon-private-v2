import time, subprocess, sys

# Test: curl vs httpx timing
# Also test a simple endpoint that returns {} to isolate uvicorn overhead

# Test 1: /api/versions (no DB, should be fast)
print("=== Simple endpoint: /api/versions ===")
for i in range(3):
    start = time.monotonic()
    r = subprocess.run(
        ['curl', '-s', 'http://localhost:8001/api/versions'],
        capture_output=True, text=True, timeout=10
    )
    elapsed = time.monotonic() - start
    print(f'  curl run {i+1}: {elapsed*1000:.0f}ms, len={len(r.stdout)}')

print()

# Test 2: /api/performance (DB query)
print("=== DB endpoint: /api/performance ===")
for i in range(2):
    start = time.monotonic()
    r = subprocess.run(
        ['curl', '-s', 'http://localhost:8001/api/performance?period=7d'],
        capture_output=True, text=True, timeout=10
    )
    elapsed = time.monotonic() - start
    print(f'  curl run {i+1}: {elapsed*1000:.0f}ms, len={len(r.stdout)}')

print()

# Test 3: /api/recommendations
print("=== /api/recommendations ===")
for i in range(3):
    start = time.monotonic()
    r = subprocess.run(
        ['curl', '-s', 'http://localhost:8001/api/recommendations?limit=20'],
        capture_output=True, text=True, timeout=15
    )
    elapsed = time.monotonic() - start
    print(f'  curl run {i+1}: {elapsed*1000:.0f}ms, len={len(r.stdout)}')

print()
print("If /api/versions is also ~2.3s: uvicorn overhead")
print("If /api/versions is fast but /api/recommendations is slow: DB bottleneck")
