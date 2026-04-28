import urllib.request, json, time

# 3-C: recommendations latency
print("=== 3-C: recommendations latency ===")
for i in range(3):
    start = time.monotonic()
    r = urllib.request.urlopen('http://localhost:8001/api/recommendations?limit=20', timeout=15)
    data = json.load(r)
    elapsed = time.monotonic() - start
    print(f'  Run {i+1}: {elapsed:.3f}s trades={len(data)}')

# 3-E: rvf snapshot
print()
print("=== 3-E: rvf snapshot ===")
try:
    r = urllib.request.urlopen('http://localhost:8001/api/rvf/snapshot', timeout=5)
    d = json.load(r)
    c = d.get('consensus', {})
    total = c.get('markets_consensus_total', 0)
    ready = c.get('markets_consensus_ready', 0)
    print(f'  total={total} ready={ready}')
    assert total >= ready and ready >= 10
    print('  PASS')
except Exception as e:
    print(f'  ERROR: {e}')

# 3-G: Trade List data quality
print()
print("=== 3-G: Trade List data quality ===")
r = urllib.request.urlopen('http://localhost:8001/api/recommendations?limit=5', timeout=15)
data = json.load(r)
trades = data
issues = [t for t in trades if not isinstance(t.get('confidence'), (int, float)) and t.get('confidence') is not None]
print(f'  trades={len(trades)} type_issues={len(issues)}')
if issues:
    for t in issues:
        print(f'    {t}')
print('  PASS')
