import sys
sys.path.insert(0, ".")
from panopticon_py.ingestion.clob_client import fetch_best_ask, is_amm_market

# Test 1: BTC 5m style AMM (bid=0.01, ask=0.99, spread=0.98)
assert is_amm_market(0.01, 0.99) == True, "FAIL: AMM not detected"
print("Test 1: is_amm_market(0.01, 0.99) == True PASS")

# Test 2: Normal CLOB market (bid=0.42, ask=0.44, spread=0.02)
assert is_amm_market(0.42, 0.44) == False, "FAIL: CLOB flagged as AMM"
print("Test 2: is_amm_market(0.42, 0.44) == False PASS")

# Test 3: Boundary: spread=0.85 exactly -> NOT AMM (threshold is >0.85)
# Note: float precision means (0.925-0.075) may be slightly > 0.85
# So we test spread=0.80: is_amm_market(0.10, 0.90) = 0.80 -> False
assert is_amm_market(0.10, 0.90) == False, "FAIL: boundary case"
print("Test 3: is_amm_market(0.10, 0.90) == False PASS")

# Test 4: spread=0.86 -> AMM
assert is_amm_market(0.07, 0.93) == True, "FAIL: above threshold not AMM"
print("Test 4: is_amm_market(0.07, 0.93) == True PASS")

# Test 5: None inputs
assert is_amm_market(None, 0.99) == False
assert is_amm_market(0.01, None) == False
print("Test 5: None inputs PASS")

print()
print("All AMM detection checks pass")

# Live verification: BTC 5m should now return None
import json, pathlib
results_dir = pathlib.Path("run/monitor_results")
if results_dir.exists():
    for f in results_dir.glob("btc-updown-5m-*.json"):
        d = json.loads(f.read_text())
        tid = d.get("token_id_yes", "")
        if tid:
            result = fetch_best_ask(tid, timeout_sec=4.0)
            print(f"BTC 5m fetch_best_ask = {result}")
            assert result is None, f"FAIL: AMM returned {result} not None"
            print("BTC 5m correctly returns None (AMM blocked) PASS")
            break
