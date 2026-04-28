import urllib.request
import json

try:
    with urllib.request.urlopen('http://localhost:8001/api/versions', timeout=5) as resp:
        data = json.load(resp)
    all_match = True
    for k, v in data.items():
        vm = v.get('version_match', False)
        ver = v.get('version', '?')
        status = 'PASS' if vm else 'FAIL'
        print(f'{status} {k}: {ver}')
        if not vm:
            all_match = False
    print(f'\nAll versions match: {all_match}')
except Exception as e:
    print(f'Error: {e}')