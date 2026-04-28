with open('panopticon_py/api/app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i, line in enumerate(lines[26:32], 27):
    leading = len(line) - len(line.lstrip())
    print(f'Line {i}: [{leading} spaces] {repr(line[:60])}')
