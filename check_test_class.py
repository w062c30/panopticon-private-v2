import re
with open("tests/test_d71_btc5m_resolver.py", "r", encoding="utf-8") as f:
    src = f.read()
classes = re.findall(r"^class (Test\w+)", src, re.MULTILINE)
print(f"Classes: {classes}")
print(f"Count: {len(classes)}")
