import httpx, time

# 3-C: recommendations latency
start = time.monotonic()
r = httpx.get('http://localhost:8001/api/recommendations?limit=20', timeout=5.0)
elapsed = time.monotonic() - start
count = len(r.json().get('trades', []))
print(f'time={elapsed:.3f}s trades={count}')
assert elapsed < 2.0, f'REGRESSION: {elapsed:.1f}s > 2s baseline'
target = 'TARGET MET <0.5s' if elapsed < 0.5 else ('OK <2s' if elapsed < 2 else 'SLOW')
print(f'Latency check: {target}')
print()

# 3-G: Trade List types
r2 = httpx.get('http://localhost:8001/api/recommendations?limit=5', timeout=5.0)
trades = r2.json().get('trades', [])
for t in trades[:2]:
    c = t.get('confidence')
    assert isinstance(c, (int, float)), f'BAD confidence: {c}'
    print(f'conf={c*100:.1f}% src={t.get("source")} status={t.get("status")} ✅')
print(f'Types check: PASS {len(trades)} trades')
print()

# 3-F: markets_consensus
r3 = httpx.get('http://localhost:8001/api/rvf/snapshot', timeout=5.0)
d = r3.json()
c = d.get('consensus', {})
total = c.get('markets_consensus_total', 0)
ready = c.get('markets_consensus_ready', 0)
assert total >= ready and ready >= 10, f'REGRESSION total={total} ready={ready}'
print(f'consensus: total={total} ready={ready} ✅')
print()

print('ALL CHECKS PASSED!')
