import subprocess, json

r2 = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe'],
                    capture_output=True, text=True)
print(r2.stdout[:1000])

with open('run/process_manifest.json') as f:
    m = json.load(f)
be = m.get('backend', {})
print(f'Manifest backend: PID={be.get("pid")} v={be.get("version")} match={be.get("version_match")}')

import urllib.request
try:
    with urllib.request.urlopen('http://localhost:8001/api/versions', timeout=5) as resp:
        data = json.load(resp)
    print('API versions:')
    for k, v in data.items():
        print(f'  {k}: {v.get("version")} expected={v.get("expected")} match={v.get("version_match")}')
except Exception as e:
    print('API error:', e)
