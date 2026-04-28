import unittest

from panopticon_py.decision_guard import SignalArtifactMeta, assert_signal_artifacts_contract


class DecisionGuardTests(unittest.TestCase):
    def test_accepts_whitelisted_artifacts(self) -> None:
        assert_signal_artifacts_contract(
            [
                SignalArtifactMeta(
                    name="l1_event",
                    source="sensor_layer",
                    timestamp="2026-04-21T00:00:00Z",
                    version="v0",
                )
            ],
            allowed_sources={"sensor_layer", "cognitive_layer", "friction_state"},
        )

    def test_rejects_graphify_source(self) -> None:
        with self.assertRaises(ValueError):
            assert_signal_artifacts_contract(
                [
                    SignalArtifactMeta(
                        name="viz",
                        source="graphify_report",
                        timestamp="2026-04-21T00:00:00Z",
                        version="v0",
                    )
                ],
                allowed_sources={"graphify_report"},
            )


if __name__ == "__main__":
    unittest.main()
