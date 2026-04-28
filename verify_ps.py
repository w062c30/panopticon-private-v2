import re

with open('scripts/restart_all.ps1', encoding='utf-8') as f:
    content = f.read()

# Find $PID references
for i, line in enumerate(content.splitlines(), 1):
    stripped = line.strip()
    if stripped.startswith('$pid ') or '$pid=' in stripped or ' $pid ' in stripped or stripped.startswith('$pid='):
        print(f'Line {i}: {stripped[:80]}')

print()
print('PowerShell $PID is readonly:', '$PID is a read-only automatic variable' in 'USE ONLY')
