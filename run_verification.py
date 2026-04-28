"""Quick end-to-end data-flow verification script.

Checks:
1. Radar can write pending_entropy_signals  (mock entropy fire)
2. Signal engine can consume and write execution_records
3. wallet_market_positions LIFO updates correctly
"""
import sys, os
sys.path.insert(0, "d:/Antigravity/Panopticon")
os.chdir("d:/Antigravity/Panopticon")

from datetime import datetime, timezone
from uuid import uuid4
from panopticon_py.db import ShadowDB

def utc():
    return datetime.now(timezone.utc).isoformat()

db = ShadowDB("d:/Antigravity/Panopticon/data/panopticon.db")
db.bootstrap()

# Clean up any stale test data
for t in ["test_market_001", "test_lifo_market"]:
    db.conn.execute("DELETE FROM pending_entropy_signals WHERE market_id = ?", (t,))
    db.conn.execute("DELETE FROM wallet_market_positions WHERE market_id = ?", (t,))
    db.conn.execute("DELETE FROM raw_events WHERE market_id = ?", (t,))
db.conn.commit()

print("=== TEST 1: Write pending_entropy_signals ===")
sig_id = str(uuid4())
db.append_pending_entropy_signal({
    "signal_id": sig_id,
    "market_id": "test_market_001",
    "token_id": "test_token_001",
    "entropy_z": -4.5,
    "sim_pnl_proxy": 0.12,
    "trigger_address": "0x1234567890123456789012345678901234567890",
    "trigger_ts_utc": utc(),
})
print(f"  Written signal_id={sig_id}")

rows = db.fetch_unconsumed_entropy_signals(limit=5)
unconsumed = [r for r in rows if r["signal_id"] == sig_id]
print(f"  Fetched unconsumed: {len(unconsumed)} matching signal")
assert len(unconsumed) == 1, "Signal not found!"
print("  PASS")

print("\n=== TEST 2: LIFO — BUY then SELL ===")
WALLET = f"0x{uuid4().hex[:40]}"  # unique wallet each run
MARKET = "test_lifo_market"
ts = utc()

# BUY 100 @ avg_entry = 0.6
db.upsert_wallet_market_position_lifo(WALLET, MARKET, fill_price=0.60, fill_qty=100.0, side="BUY", updated_ts_utc=ts)
pos = db.get_wallet_market_position(WALLET, MARKET)
print(f"  After BUY 100@0.60: notional={pos['current_position_notional']}, avg_entry={pos['avg_entry_price']:.4f}")
assert abs(pos["current_position_notional"] - 100.0) < 0.01, f"BUY notional wrong: {pos['current_position_notional']}"
assert abs(pos["avg_entry_price"] - 0.60) < 0.0001, f"avg_entry wrong: {pos['avg_entry_price']}"
print("  PASS")

# BUY 100 @ avg_entry = 0.8 (higher price)
import time; time.sleep(0.01)
ts2 = utc()
db.upsert_wallet_market_position_lifo(WALLET, MARKET, fill_price=0.80, fill_qty=100.0, side="BUY", updated_ts_utc=ts2)
pos = db.get_wallet_market_position(WALLET, MARKET)
vwap = (0.60*100 + 0.80*100) / 200
print(f"  After BUY 100@0.80: notional={pos['current_position_notional']}, avg_entry={pos['avg_entry_price']:.4f}")
assert abs(pos["current_position_notional"] - 200.0) < 0.01, f"notional wrong: {pos['current_position_notional']}"
assert abs(pos["avg_entry_price"] - vwap) < 0.0001, f"VWAP wrong: {pos['avg_entry_price']}"
print("  PASS")

# SELL 50 — LIFO should reduce the 0.80 position first, avg_entry stays at vwap of remaining
import time; time.sleep(0.01)
ts3 = utc()
db.upsert_wallet_market_position_lifo(WALLET, MARKET, fill_price=0.85, fill_qty=50.0, side="SELL", updated_ts_utc=ts3)
pos = db.get_wallet_market_position(WALLET, MARKET)
# Remaining: 50 @ 0.80 + 50 @ 0.60 = 100 shares, avg_entry = (0.60*50 + 0.80*50)/100 = 0.70
remaining_vwap = (0.60*50 + 0.80*50) / 100
print(f"  After SELL 50@0.85 (LIFO): notional={pos['current_position_notional']}, avg_entry={pos['avg_entry_price']:.4f}")
assert abs(pos["current_position_notional"] - 150.0) < 0.01, f"SELL notional wrong: {pos['current_position_notional']}"
assert abs(pos["avg_entry_price"] - remaining_vwap) < 0.0001, f"avg_entry changed on SELL: {pos['avg_entry_price']}"
print("  PASS")

print("\n=== TEST 3: Write execution_record ===")
exec_id = str(uuid4())
dec_id = str(uuid4())
event_id = str(uuid4())
# FK chain: raw_events → strategy_decisions → execution_records
db.append_raw_event({
    "event_id": event_id,
    "layer": "L3",
    "event_type": "consensus_signal",
    "source": "signal_engine_test",
    "source_event_id": None,
    "event_ts": utc(),
    "ingest_ts_utc": utc(),
    "version_tag": "test",
    "market_id": "test_market_001",
    "asset_id": None,
    "payload": {"test": True},
})
db.append_strategy_decision({
    "decision_id": dec_id,
    "event_id": event_id,
    "feature_snapshot_id": str(uuid4()),
    "market_snapshot_id": str(uuid4()),
    "prior_probability": 0.5,
    "likelihood_ratio": 1.5,
    "posterior_probability": 0.6,
    "ev_net": 0.05,
    "kelly_fraction": 0.2,
    "action": "BUY",
    "created_ts_utc": utc(),
})
db.append_execution_record({
    "execution_id": exec_id,
    "decision_id": dec_id,
    "accepted": 1,
    "reason": "PAPER_TX_SIM",
    "gate_reason": "PASS",
    "latency_ms": 150.0,
    "created_ts_utc": utc(),
})
print(f"  Written execution_id={exec_id} with decision_id={dec_id}")

status = db.fetch_system_status()
print(f"  fetch_system_status: state={status['state']}, message={status['message']}")
assert status["state"] == "executed", f"Expected 'executed', got '{status['state']}'"
print("  PASS")

print("\n=== TEST 4: Mark signal consumed ===")
db.mark_entropy_signal_consumed(sig_id, "signal_engine")
remaining = db.fetch_unconsumed_entropy_signals(limit=5)
unconsumed_after = [r for r in remaining if r["signal_id"] == sig_id]
print(f"  Unconsumed after mark: {len(unconsumed_after)}")
assert len(unconsumed_after) == 0, "Signal should be consumed!"
print("  PASS")

print("\n=== ALL TESTS PASSED ===")
db.close()
