import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from panopticon_py.strategy.bayesian_engine import PortfolioPosition, check_cluster_exposure_limit


class TestBayesianRhoFallback(unittest.TestCase):
    def test_same_cluster_opposed_direction_can_hedge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mapping = {
                "m_target": {"cluster_id": "US_Election_2024", "internal_direction": -1},
                "m_active": {"cluster_id": "US_Election_2024", "internal_direction": 1},
            }
            p = Path(td) / "cluster_mapping.json"
            p.write_text(json.dumps(mapping), encoding="utf-8")
            with patch.dict("os.environ", {"CLUSTER_MAPPING_PATH": p.as_posix()}, clear=False):
                ok, reason, audit = check_cluster_exposure_limit(
                    target_market="m_target",
                    proposed_signed_notional_usd=80.0,
                    portfolio=[PortfolioPosition("m_active", 200.0)],
                    cluster_map={"m_target": "US_Election_2024", "m_active": "US_Election_2024"},
                    cluster_anchor={"US_Election_2024": "m_target"},
                    correlation_matrix={},
                    total_capital_usd=1_000.0,
                    cluster_cap_fraction=0.25,
                )
            self.assertTrue(ok)
            self.assertEqual(reason, "HEDGE_REDUCES_ABS_NET_DELTA")
            self.assertLess(abs(audit.net_delta), 200.0)

    def test_same_cluster_same_direction_rejected_over_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            mapping = {
                "m_target": {"cluster_id": "US_Election_2024", "internal_direction": 1},
                "m_active": {"cluster_id": "US_Election_2024", "internal_direction": 1},
            }
            p = Path(td) / "cluster_mapping.json"
            p.write_text(json.dumps(mapping), encoding="utf-8")
            with patch.dict("os.environ", {"CLUSTER_MAPPING_PATH": p.as_posix()}, clear=False):
                ok, reason, _ = check_cluster_exposure_limit(
                    target_market="m_target",
                    proposed_signed_notional_usd=80.0,
                    portfolio=[PortfolioPosition("m_active", 200.0)],
                    cluster_map={"m_target": "US_Election_2024", "m_active": "US_Election_2024"},
                    cluster_anchor={"US_Election_2024": "m_target"},
                    correlation_matrix={},
                    total_capital_usd=1_000.0,
                    cluster_cap_fraction=0.25,
                )
            self.assertFalse(ok)
            self.assertEqual(reason, "CLUSTER_EXPOSURE_CAP")

