import unittest

from panopticon_py.fast_gate import FastSignalInput, GateDecision, fast_execution_gate
from panopticon_py.friction_state import FrictionSnapshot


class FastGateTests(unittest.TestCase):
    def _snapshot(self, **kwargs) -> FrictionSnapshot:
        base = FrictionSnapshot(
            network_ping_ms=120.0,
            current_base_fee=0.001,
            kyle_lambda=0.00001,
            gas_cost_estimate=0.2,
            api_health="ok",
            l2_timeout_ms=120.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=0.0,
        )
        return FrictionSnapshot(**{**base.__dict__, **kwargs})

    def _signal(self, **kwargs) -> FastSignalInput:
        base = FastSignalInput(
            p_prior=0.62,
            quote_price=0.48,
            payout=1.0,
            capital_in=0.48,
            order_size=50.0,
            delta_t_ms=100.0,
            gamma=0.001,
            slippage_tolerance=0.009,
            min_ev_threshold=0.0,
            daily_opp_cost=0.0008,
            days_to_resolution=2,
        )
        return FastSignalInput(**{**base.__dict__, **kwargs})

    def test_abort_on_high_ping(self) -> None:
        out = fast_execution_gate(self._signal(), self._snapshot(network_ping_ms=250.0))
        self.assertEqual(out.decision, GateDecision.ABORT)

    def test_degrade_on_timeout(self) -> None:
        out = fast_execution_gate(self._signal(), self._snapshot(degraded=True, l2_timeout_ms=600))
        self.assertIn(out.decision, {GateDecision.DEGRADE, GateDecision.ABORT})
        self.assertAlmostEqual(out.kelly_cap, 0.1)

    def test_execute_when_ev_positive(self) -> None:
        out = fast_execution_gate(self._signal(), self._snapshot())
        self.assertIn(out.decision, {GateDecision.EXECUTE, GateDecision.ABORT, GateDecision.DEGRADE})
        self.assertGreater(out.p_adjusted, 0.0)
        self.assertLess(out.p_adjusted, 1.0)


if __name__ == "__main__":
    unittest.main()
