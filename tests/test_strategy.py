import unittest

from panopticon_py.strategy import StrategyInput, bayesian_update, decide, fractional_kelly


class StrategyTests(unittest.TestCase):
    def test_bayesian_update_increases_with_lr(self) -> None:
        prior = 0.5
        p1 = bayesian_update(prior, 0.8)
        p2 = bayesian_update(prior, 1.2)
        self.assertLess(p1, prior)
        self.assertGreater(p2, prior)

    def test_fractional_kelly_non_negative(self) -> None:
        kelly = fractional_kelly(0.4, 0.55, 0.25)
        self.assertGreaterEqual(kelly, 0.0)

    def test_decide_buy_when_ev_positive(self) -> None:
        out = decide(
            StrategyInput(
                prior_probability=0.55,
                likelihood_ratio=1.3,
                price=0.45,
                fee_rate=0.001,
                slippage_pct=0.001,
                alpha=0.25,
            )
        )
        self.assertIn(out.action, {"BUY", "HOLD"})
        self.assertGreaterEqual(out.posterior_probability, 0.0)
        self.assertLessEqual(out.posterior_probability, 1.0)


if __name__ == "__main__":
    unittest.main()
