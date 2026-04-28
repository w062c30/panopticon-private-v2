"""Verify FastGate with avg_entry_price (LIFO cost basis)."""
import sys
sys.path.insert(0, "d:/Antigravity/Panopticon")

from panopticon_py.fast_gate import FastSignalInput, fast_execution_gate, GateDecision
from panopticon_py.friction_state import FrictionSnapshot

snap = FrictionSnapshot(
    network_ping_ms=100.0,
    current_base_fee=0.001,
    kyle_lambda=0.00001,
    gas_cost_estimate=0.2,
    api_health="ok",
    l2_timeout_ms=200.0,
    degraded=False,
    kelly_cap=0.25,
    last_update_ts=0.0,
)

# EV formula:
#   EV_net = p_adj * payout * qty - capital_in - avg_entry*qty - taker_fee - gas - slippage
# For Polymarket: payout=1.0, so EV_net = p_adj*qty - capital_in - avg_entry*qty - friction
# With order_size=$500, qty=500 units at price 0.50 => position cost = $250 at current quote
def make_sig(p_prior, avg_entry_price, order_size_usd=500.0, quote_price=0.50):
    order_qty = order_size_usd  # units (notional = qty * price)
    capital_in = quote_price * order_qty  # cost to acquire order_qty units at current price
    return FastSignalInput(
        p_prior=p_prior,
        quote_price=quote_price,
        payout=1.0,
        capital_in=capital_in,
        order_size=order_qty,
        avg_entry_price=avg_entry_price,
        delta_t_ms=100.0,
        gamma=0.001,
        slippage_tolerance=0.009,
        min_ev_threshold=0.0,
        daily_opp_cost=0.0008,
        days_to_resolution=3.0,
        bid_ask_imbalance=0.0,
    )

# TEST A: No existing position (avg_entry=0), high confidence — should EXECUTE
resultA = fast_execution_gate(make_sig(0.75, 0.0), snap)
print(f"[A] avg_entry=0, p=0.75, order_qty=500@0.50: decision={resultA.decision.value} ev_net={resultA.ev_net:.4f} reason={resultA.reason}")
assert resultA.decision == GateDecision.EXECUTE, f"A should EXECUTE, got {resultA.decision} reason={resultA.reason}"

# TEST B: Existing position at 0.40, adding at 0.50 — EV should be lower (cost basis effect)
resultB = fast_execution_gate(make_sig(0.75, 0.40), snap)
print(f"[B] avg_entry=0.40, p=0.75, order_qty=500@0.50: decision={resultB.decision.value} ev_net={resultB.ev_net:.4f} reason={resultB.reason}")
assert resultB.decision == GateDecision.EXECUTE, f"B should EXECUTE, got {resultB.decision} reason={resultB.reason}"
ev_delta = resultA.ev_net - resultB.ev_net
position_cost = 0.40 * 500.0
print(f"  EV reduction: {ev_delta:.4f} (expected ~= {position_cost:.4f} from avg_entry*qty)")
assert abs(ev_delta - position_cost) < 0.01, f"EV delta should equal avg_entry*qty: {ev_delta} vs {position_cost}"
print("  PASS: avg_entry correctly deducts existing cost basis from EV")

# TEST C: Deep loss position (avg_entry=0.90), now quoting 0.40 — should ABORT
resultC = fast_execution_gate(make_sig(0.75, 0.90, quote_price=0.40), snap)
print(f"[C] avg_entry=0.90, quote=0.40, p=0.75: decision={resultC.decision.value} ev_net={resultC.ev_net:.4f} reason={resultC.reason}")
assert resultC.decision == GateDecision.ABORT, f"C should ABORT, got {resultC.decision}"
print("  PASS: deep loss position correctly aborted by LIFO cost basis gate")

# TEST D: Very high confidence with deep loss — still ABORT
resultD = fast_execution_gate(make_sig(0.95, 0.90, quote_price=0.40), snap)
print(f"[D] avg_entry=0.90, quote=0.40, p=0.95: decision={resultD.decision.value} ev_net={resultD.ev_net:.4f} reason={resultD.reason}")
assert resultD.decision == GateDecision.ABORT, f"D should ABORT, got {resultD.decision}"
print("  PASS: even near-certain confidence cannot overcome existing loss")

# TEST E: Normal exit — existing at 0.50, exit at 0.60 — profit on existing LIFO position
resultE = fast_execution_gate(make_sig(0.80, 0.50, order_size_usd=100.0, quote_price=0.60), snap)
print(f"[E] avg_entry=0.50, quote=0.60, p=0.80: decision={resultE.decision.value} ev_net={resultE.ev_net:.4f} reason={resultE.reason}")
assert resultE.decision == GateDecision.EXECUTE, f"E should EXECUTE, got {resultE.decision}"
print("  PASS: profitable re-entry/addition allowed")

print("\nAll FastGate LIFO cost basis tests passed!")


