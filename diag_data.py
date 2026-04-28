import urllib.request, json

r = urllib.request.urlopen('http://localhost:8001/api/recommendations?limit=5', timeout=15)
raw = r.read()
print('Raw length:', len(raw))
print('Raw preview:', raw[:200])

data = json.loads(raw)
print()
print('Top-level keys:', list(data.keys()))
print('Is list:', isinstance(data, list))
print('Is dict:', isinstance(data, dict))
if isinstance(data, dict):
    trades = data.get('trades', [])
    print('Trades count:', len(trades))
    if trades:
        print('First trade keys:', list(trades[0].keys()))
        print('First trade confidence:', trades[0].get('confidence'))
        issues = [t for t in trades if t.get('confidence') is not None and not isinstance(t.get('confidence'), (int, float))]
        print(f'  Type issues: {len(issues)}')
