import unittest

from panopticon_py.strategy.bayesian_engine import (
    BayesianEngine,
    PortfolioPosition,
    check_cluster_exposure_limit,
    enforce_cluster_limit_or_raise,
    net_delta_for_cluster,
)
from panopticon_py.strategy.iron_rules import (
    ClusterCapFrictionRebalanceError,
    ClusterExposureCapError,
    StaticPnLBypassError,
    assert_no_macro_bypass,
    assert_no_parent_sub_friction_rebalance,
)


class TestBayesianCluster(unittest.TestCase):
    def test_net_delta_rho_default_one(self) -> None:
        pmap = {"a": "C1", "b": "C1"}
        anchor = {"C1": "a"}
        port = [
            PortfolioPosition("a", 10_000),
            PortfolioPosition("b", -5_000),
        ]
        nd, _ = net_delta_for_cluster("C1", port, pmap, anchor, {})
        self.assertAlmostEqual(nd, 10_000 - 5_000)

    def test_cluster_cap_rejects_add(self) -> None:
        cmap = {"m1": "US", "m2": "US"}
        port = [PortfolioPosition("m1", 240_000)]
        ok, reason, _ = check_cluster_exposure_limit(
            "m2",
            20_000,
            port,
            cmap,
            {"US": "m1"},
            {},
            total_capital_usd=1_000_000,
            cluster_cap_fraction=0.25,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "CLUSTER_EXPOSURE_CAP")

    def test_hedge_reduces_abs_net_allowed(self) -> None:
        cmap = {"m1": "US", "m2": "US"}
        port = [PortfolioPosition("m1", 250_000)]
        ok, reason, audit = check_cluster_exposure_limit(
            "m2",
            -30_000,
            port,
            cmap,
            {"US": "m1"},
            {},
            total_capital_usd=1_000_000,
            cluster_cap_fraction=0.25,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "HEDGE_REDUCES_ABS_NET_DELTA")
        self.assertLess(abs(audit.net_delta), 250_000)

    def test_unknown_five_percent_cap(self) -> None:
        ok, reason, _ = check_cluster_exposure_limit(
            "m_unknown",
            60_000,
            [],
            {},
            {},
            {},
            total_capital_usd=1_000_000,
            unknown_individual_cap_fraction=0.05,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "UNKNOWN_CLUSTER_5PCT_CAP")

    def test_enforce_raises(self) -> None:
        with self.assertRaises(ClusterExposureCapError):
            enforce_cluster_limit_or_raise(
                "m2",
                50_000,
                [PortfolioPosition("m1", 240_000)],
                {"m1": "US", "m2": "US"},
                {"US": "m1"},
                {},
                total_capital_usd=1_000_000,
            )

    def test_bayesian_engine_posterior_cap(self) -> None:
        eng = BayesianEngine({"m": "C"}, {})
        p, audit = eng.calculate_posterior(0.5, -5.0, 2.0)
        self.assertLessEqual(p, 0.99)
        self.assertIn("lr_consensus", audit)

    def test_iron_rules_assertions(self) -> None:
        with self.assertRaises(StaticPnLBypassError):
            assert_no_macro_bypass("MACRO_HARVEST", False)
        with self.assertRaises(ClusterCapFrictionRebalanceError):
            assert_no_parent_sub_friction_rebalance(
                sell_market_id="parent_m",
                buy_market_id="child_m",
                cluster_map={"parent_m": "K", "child_m": "K"},
                market_roles={"parent_m": "parent", "child_m": "child"},
                cluster_cap_breached=True,
            )
