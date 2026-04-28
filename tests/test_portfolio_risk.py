import unittest

from panopticon_py.portfolio_risk import Position, allocate_kelly_with_correlation


class PortfolioRiskTests(unittest.TestCase):
    def test_shares_kelly_budget_when_correlated_inventory(self) -> None:
        inv = [Position(market_id="m1", cluster_id="c1", kelly_fraction=0.2)]
        capped = allocate_kelly_with_correlation(
            proposed_kelly=0.25,
            cluster_id="c1",
            correlation=0.95,
            inventory=inv,
        )
        self.assertLess(capped, 0.25)

    def test_no_inventory_uses_full_proposal(self) -> None:
        full = allocate_kelly_with_correlation(
            proposed_kelly=0.25,
            cluster_id="c1",
            correlation=0.95,
            inventory=[],
        )
        self.assertEqual(full, 0.25)


if __name__ == "__main__":
    unittest.main()
