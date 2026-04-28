import os
import unittest

from panopticon_py.hunting.discovery_loop import _resolve_discovery_interval_hours
from scripts.start_shadow_hydration import ensure_shadow_mode_env


class ShadowHydrationSopTests(unittest.TestCase):
    def test_interval_relax_by_tier1(self) -> None:
        hours, reason = _resolve_discovery_interval_hours(
            elapsed_hours=1.0,
            tier1_count=120,
            cold_start_hours=48.0,
            cold_start_interval_hours=2.0,
            relaxed_interval_hours=6.0,
            tier1_threshold=100,
        )
        self.assertEqual(hours, 6.0)
        self.assertEqual(reason, "tier1_threshold")

    def test_interval_relax_by_time(self) -> None:
        hours, reason = _resolve_discovery_interval_hours(
            elapsed_hours=49.0,
            tier1_count=10,
            cold_start_hours=48.0,
            cold_start_interval_hours=2.0,
            relaxed_interval_hours=6.0,
            tier1_threshold=100,
        )
        self.assertEqual(hours, 6.0)
        self.assertEqual(reason, "time_window_elapsed")

    def test_interval_cold_start(self) -> None:
        hours, reason = _resolve_discovery_interval_hours(
            elapsed_hours=2.0,
            tier1_count=10,
            cold_start_hours=48.0,
            cold_start_interval_hours=2.0,
            relaxed_interval_hours=6.0,
            tier1_threshold=100,
        )
        self.assertEqual(hours, 2.0)
        self.assertEqual(reason, "cold_start_window")

    def test_runner_guard_live_trading(self) -> None:
        old = os.environ.get("LIVE_TRADING")
        os.environ["LIVE_TRADING"] = "true"
        try:
            with self.assertRaises(RuntimeError):
                ensure_shadow_mode_env()
        finally:
            if old is None:
                os.environ.pop("LIVE_TRADING", None)
            else:
                os.environ["LIVE_TRADING"] = old


if __name__ == "__main__":
    unittest.main()
