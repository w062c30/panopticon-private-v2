import unittest

from panopticon_py.cognitive import build_cognitive_signal


class CognitiveSecondOrderTests(unittest.TestCase):
    def test_cex_funding_disables_graph_and_keeps_scores_bounded(self) -> None:
        out = build_cognitive_signal(
            wallet_features={
                "funding_depth": 0.8,
                "split_order_score": 0.7,
                "funding_source_label": "binance_hot_wallet",
                "temporal_hour_utc": 3,
                "temporal_minute_utc": 10,
                "has_preflight_tx": True,
            },
            headline="demo",
            use_llm=False,
        )
        self.assertTrue(out.graph_disabled)
        self.assertGreaterEqual(out.behavior_score, 0.0)
        self.assertLessEqual(out.behavior_score, 1.0)
        self.assertGreaterEqual(out.trust_score, 0.0)
        self.assertLessEqual(out.trust_score, 1.0)


if __name__ == "__main__":
    unittest.main()
