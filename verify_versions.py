import re, json

files = {
    'panopticon_py/api/app.py': r'PROCESS_VERSION\s*=\s*["\']([^"\']+)["\']',
    'panopticon_py/hunting/run_radar.py': r'PROCESS_VERSION\s*=\s*["\']([^"\']+)["\']',
    'run_hft_orchestrator.py': r'PROCESS_VERSION\s*=\s*["\']([^"\']+)["\']',
}
for path, pattern in files.items():
    try:
        content = open(path, encoding='utf-8').read()
        m = re.search(pattern, content)
        print(f'{path}: {m.group(1) if m else "NOT FOUND"}')
    except FileNotFoundError:
        print(f'{path}: FILE NOT FOUND')

pkg = json.load(open('dashboard/package.json', encoding='utf-8'))
print(f'dashboard/package.json: {pkg.get("version","NOT FOUND")}')

ref = json.load(open('run/versions_ref.json', encoding='utf-8'))
for k, v in ref.items():
    if isinstance(v, dict):
        print(f'versions_ref[{k}]: {v.get("version","?")}')
    else:
        print(f'versions_ref[{k}]: {v}')
