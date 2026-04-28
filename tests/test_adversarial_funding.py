import unittest

from panopticon_py.adversarial_funding import classify_funding_source, should_disable_graph_trace


class AdversarialFundingTests(unittest.TestCase):
    def test_mixer_disables_graph(self) -> None:
        probe = classify_funding_source("tornado_cash_relay")
        self.assertTrue(should_disable_graph_trace(probe))

    def test_cex_hot_disables_graph(self) -> None:
        probe = classify_funding_source("binance_hot_wallet")
        self.assertTrue(should_disable_graph_trace(probe))

    def test_normal_keeps_graph(self) -> None:
        probe = classify_funding_source("direct_bridge")
        self.assertFalse(should_disable_graph_trace(probe))


if __name__ == "__main__":
    unittest.main()
