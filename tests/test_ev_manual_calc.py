"""Manual EV calculation verification — does NOT depend on DB or WS.

Diagnoses ev_net = -4975 by computing EV with typical Polymarket parameters.
If this test PASS (ev_net in plausible range): formula is correct, problem is in actual params.
If this test FAIL (ev_net = -4975): formula itself has a bug.
"""
import math
import unittest

from panopticon_py.fast_gate import FastSignalInput, fast_execution_gate
from panopticon_py.friction_state import FrictionSnapshot


class TestEVManualCalculation(unittest.TestCase):
    """Task B: Manual EV formula verification."""

    def test_ev_with_typical_polymarket_params(self) -> None:
        """
        Polymarket binary market, typical params:
          payout = 1.0 (YES token settles at $1/share)
          quote_price = 0.60 (current market price)
          order_size = 5 shares
          capital_in = 5 × 0.60 = 3.00 USDC
          posterior = 0.70 (we believe YES probability is 70%)

        Expected: ev_net ≈ (0.70 × 1.0 × 5) - 3.00 = 0.50 USDC
        If ev_net = -4975 → default values in actual params are wrong
        """
        snapshot = FrictionSnapshot(
            network_ping_ms=50.0,
            current_base_fee=0.001,
            kyle_lambda=0.001,
            gas_cost_estimate=0.001,
            api_health="ok",
            l2_timeout_ms=1000.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=0.0,
        )

        # Typical Polymarket params (not the system defaults)
        inp = FastSignalInput(
            p_prior=0.70,
            quote_price=0.60,
            payout=1.0,             # Polymarket binary = 1.0
            capital_in=3.00,       # 5 shares × $0.60
            order_size=5,
            avg_entry_price=0.0,    # first entry
            delta_t_ms=120.0,
            gamma=0.001,
            slippage_tolerance=0.01,
            min_ev_threshold=0.0,
            daily_opp_cost=0.0008,
            days_to_resolution=3.0,
            bid_ask_imbalance=0.0,
        )

        gate = fast_execution_gate(inp, snapshot)

        print(f"\n[TEST EV] ev_net = {gate.ev_net}")
        print(f"[TEST EV] ev_time_adj = {gate.ev_time_adj}")
        print(f"[TEST EV] p_adjusted = {gate.p_adjusted}")
        print(f"[TEST EV] decision = {gate.decision.name}")
        print(f"[TEST EV] reason = {gate.reason}")

        # ev_net should be around 0.50 for plausible params
        # -4975 would indicate a unit mismatch (e.g., capital_in = 5000 instead of 3.00)
        self.assertGreater(
            gate.ev_net, -10, f"ev_net magnitude异常: {gate.ev_net} — 可能capital_in单位错误"
        )
        self.assertLess(
            gate.ev_net, 10, f"ev_net magnitude异常: {gate.ev_net} — 请检查payout/capital_in"
        )

    def test_ev_with_system_default_params(self) -> None:
        """
        Using the actual system default values from signal_engine._process_event():
          order_size_usd = DEFAULT_CAPITAL * KELLY_FRACTION = 100 * 0.25 = 25.0
          capital_in = current_price * order_size_usd
          delta_t_ms = 150.0
          gamma = 0.001
          daily_opp_cost = 0.0008
          days_to_resolution = 3.0

        With typical price 0.60 and posterior 0.70:
          order_size_usd = 25.0
          capital_in = 0.60 * 25.0 = 15.00 USDC
          Expected ev_net ≈ (0.70 × 1.0 × 25) - 15 = 2.50 USDC
        """
        snapshot = FrictionSnapshot(
            network_ping_ms=50.0,
            current_base_fee=0.001,
            kyle_lambda=0.001,
            gas_cost_estimate=0.001,
            api_health="ok",
            l2_timeout_ms=1000.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=0.0,
        )

        # System default params
        inp = FastSignalInput(
            p_prior=0.70,
            quote_price=0.60,
            payout=1.0,
            capital_in=15.00,       # 0.60 * 25.0 (order_size_usd)
            order_size=25.0,         # DEFAULT_CAPITAL * KELLY_FRACTION = 100 * 0.25
            avg_entry_price=0.0,
            delta_t_ms=150.0,
            gamma=0.001,
            slippage_tolerance=0.009,
            min_ev_threshold=0.0,
            daily_opp_cost=0.0008,
            days_to_resolution=3.0,
            bid_ask_imbalance=0.0,
        )

        gate = fast_execution_gate(inp, snapshot)

        print(f"\n[TEST SYS DEFAULTS] ev_net = {gate.ev_net}")
        print(f"[TEST SYS DEFAULTS] ev_time_adj = {gate.ev_time_adj}")
        print(f"[TEST SYS DEFAULTS] p_adjusted = {gate.p_adjusted}")
        print(f"[TEST SYS DEFAULTS] decision = {gate.decision.name}")
        print(f"[TEST SYS DEFAULTS] reason = {gate.reason}")

        self.assertGreater(gate.ev_net, -100, f"ev_net magnitude异常: {gate.ev_net}")


class TestEVShadowModeParams(unittest.TestCase):
    """Verify shadow mode parameters fix the SLIPPAGE_TOLERANCE_EXCEEDED issue."""

    def test_shadow_mode_slippage_not_exceeded(self) -> None:
        """
        Shadow mode: kyle_lambda=0.00001, order_size=10, slippage_tolerance=0.05
        expected_slippage = 10 * 0.00001 = 0.0001
        0.0001 < 0.05 → should NOT SLIPPAGE_TOLERANCE_EXCEEDED
        """
        snapshot = FrictionSnapshot(
            network_ping_ms=50.0,
            current_base_fee=0.001,
            kyle_lambda=0.00001,   # Shadow mode calibrated value
            gas_cost_estimate=0.001,
            api_health="ok",
            l2_timeout_ms=1000.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=0.0,
        )

        # Shadow mode params
        inp = FastSignalInput(
            p_prior=0.70,
            quote_price=0.60,
            payout=1.0,
            capital_in=0.60 * 10.0,  # 6.00 USDC (shadow order_size=10)
            order_size=10.0,            # Shadow mode
            avg_entry_price=0.0,
            delta_t_ms=150.0,
            gamma=0.001,
            slippage_tolerance=0.05,   # Shadow mode 5%
            min_ev_threshold=0.0,
            daily_opp_cost=0.0008,
            days_to_resolution=3.0,
            bid_ask_imbalance=0.0,
        )

        gate = fast_execution_gate(inp, snapshot)

        print(f"\n[SHADOW MODE] ev_net = {gate.ev_net}")
        print(f"[SHADOW MODE] ev_time_adj = {gate.ev_time_adj}")
        print(f"[SHADOW MODE] p_adjusted = {gate.p_adjusted}")
        print(f"[SHADOW MODE] decision = {gate.decision.name}")
        print(f"[SHADOW MODE] reason = {gate.reason}")

        self.assertNotEqual(
            gate.reason, "SLIPPAGE_TOLERANCE_EXCEEDED",
            f"Shadow mode should NOT trigger SLIPPAGE_TOLERANCE_EXCEEDED: {gate.reason}"
        )


class TestEVFormulaBreakdown(unittest.TestCase):
    """Step-by-step EV formula verification."""

    def test_ev_formula_components(self) -> None:
        """
        Verify each term in the EV formula:
          ev_net = p_adj*payout*order_size - capital_in - avg_entry*order_size - taker_fee - gas - slippage
          ev_time_adj = ev_net - capital_in * daily_opp_cost * days_to_resolution

        With typical values, each component should be in plausible range.
        """
        p_adj = 0.70 * math.exp(-0.001 * 120.0)  # ≈ 0.9163 * exp(-0.12) ≈ 0.816
        payout = 1.0
        order_size = 25.0
        capital_in = 15.00
        avg_entry = 0.0
        current_base_fee = 0.001
        quote_price = 0.60
        kyle_lambda = 0.001
        bai = 0.0
        bai_multiplier = 3.0
        gas_cost = 0.001

        p_avg = quote_price + order_size * kyle_lambda * (1.0 + bai_multiplier * bai)
        taker_fee = current_base_fee * p_avg * (1 - p_avg)
        slippage_cost = max(0.0, p_avg - quote_price)

        ev_net = (
            (p_adj * payout * order_size)
            - capital_in
            - (avg_entry * order_size)
            - taker_fee
            - gas_cost
            - slippage_cost
        )

        daily_opp_cost = 0.0008
        days_to_res = 3.0
        ev_time_adj = ev_net - (capital_in * daily_opp_cost * days_to_res)

        print(f"\n[FORMULA BREAKDOWN]")
        print(f"  p_adj          = {p_adj:.6f}")
        print(f"  p_avg          = {p_avg:.6f}")
        print(f"  taker_fee      = {taker_fee:.6f}")
        print(f"  slippage_cost = {slippage_cost:.6f}")
        print(f"  ev_net         = {ev_net:.6f}")
        print(f"  ev_time_adj    = {ev_time_adj:.6f}")

        # With these params ev_net should be positive (~2.5)
        self.assertGreater(ev_net, 0, f"ev_net should be positive with good params, got {ev_net}")
        self.assertGreater(ev_time_adj, 0, f"ev_time_adj should be positive, got {ev_time_adj}")


if __name__ == "__main__":
    unittest.main()
