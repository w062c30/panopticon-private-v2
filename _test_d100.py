import ast, subprocess, json

with open("panopticon_py/db.py", encoding="utf-8") as f:
    src = f.read()

# 1. Syntax
ast.parse(src)
print("[1] Syntax OK")

# 2. Definition count
count = src.count("def append_kyle_lambda_sample")
assert count == 1, f"Expected 1, got {count}"
print(f"[2] Definition count: {count} OK")

# 3. Buffer path
idx = src.find("def append_kyle_lambda_sample")
snippet = src[idx:idx+1200]
assert "_kyle_buffer.append" in snippet, "Missing _kyle_buffer.append"
print("[3] Buffer path exists OK")

# 4. Guards at entry
assert "trade_size <= 0" in snippet, "Missing guard trade_size"
assert "lambda_obs <= 0" in snippet, "Missing guard lambda_obs"
print("[4] Guards at entry OK")

# 5. Flush guard bypass warning
idx2 = src.find("def _flush_kyle_buffer")
flush_snippet = src[idx2:idx2+900]
assert "guard bypass detected" in flush_snippet, "Missing flush warning"
print("[5] Flush guard bypass warning OK")

# 6. EXP-D100-001 in EXPERIENCE_PLAYBOOK
with open("EXPERIENCE_PLAYBOOK.md", encoding="utf-8") as f:
    exp = f.read()
assert "EXP-D100-001" in exp, "Missing EXP-D100-001"
print("[6] EXP-D100-001 in EXPERIENCE_PLAYBOOK OK")

# 7. Version ref
with open("run/versions_ref.json", encoding="utf-8") as f:
    v = json.load(f)
assert v["orchestrator"] == "v1.1.22-D100", f"Bad version: {v['orchestrator']}"
print(f"[7] versions_ref.json orchestrator={v['orchestrator']} OK")

# 8. PROCESS_VERSION in orchestrator
with open("run_hft_orchestrator.py", encoding="utf-8") as f:
    orch_src = f.read()
assert 'PROCESS_VERSION = "v1.1.22-D100"' in orch_src, "Bad PROCESS_VERSION in orchestrator"
print("[8] run_hft_orchestrator.py PROCESS_VERSION OK")

print()
print("ALL ACCEPTANCE CHECKS PASSED")
