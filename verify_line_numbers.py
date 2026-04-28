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
        print(f'{path} line {content[:m.start()].count(chr(10))+1}: {m.group(1) if m else "NOT FOUND"}')
    except FileNotFoundError:
        print(f'{path}: FILE NOT FOUND')
