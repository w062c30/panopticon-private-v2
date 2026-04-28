import ast
with open("tests/test_d71_btc5m_resolver.py", "r", encoding="utf-8") as f:
    src = f.read()
ast.parse(src)
print("AST OK")
