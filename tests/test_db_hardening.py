import tempfile
import unittest
from pathlib import Path

from panopticon_py.db import ShadowDB


class TestDbHardening(unittest.TestCase):
    def test_positions_extended_columns_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "panopticon.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            db.append_position(
                {
                    "position_id": "pos-1",
                    "market_id": "m1",
                    "cluster_id": "c1",
                    "side": "NO",
                    "signed_notional_usd": -120.0,
                    "kelly_fraction": 0.1,
                    "opened_ts_utc": "2026-01-01T00:00:00+00:00",
                }
            )
            rows = db.fetch_open_positions_extended()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["side"], "NO")
            self.assertAlmostEqual(rows[0]["signed_notional_usd"], -120.0)
            db.close()

    def test_append_paper_trade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "panopticon.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            db.append_paper_trade(
                {
                    "paper_trade_id": "pt-1",
                    "decision_id": "d-1",
                    "wallet_address": "0x" + "aa" * 20,
                    "market_id": "m1",
                    "cluster_id": "c1",
                    "side": "YES",
                    "sizing_notional": 100.0,
                    "kelly_fraction": 0.1,
                    "cluster_delta_before": 10.0,
                    "cluster_delta_after": 20.0,
                    "reason": "DRY_RUN_PAPER_EXECUTED",
                    "outcome": "win",
                    "created_ts_utc": "2026-01-01T00:00:00+00:00",
                }
            )
            row = db.conn.execute("SELECT COUNT(*) FROM paper_trades").fetchone()
            self.assertEqual(int(row[0]), 1)
            db.close()

    def test_execution_record_insert_without_strategy_decision(self) -> None:
        """D45a: execution_records no longer has FK to strategy_decisions.
        A random decision_id UUID must insert without IntegrityError.
        """
        import uuid
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "panopticon.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            # No strategy_decisions row needed — FK removed
            random_decision_id = str(uuid.uuid4())
            db.append_execution_record({
                "execution_id": str(uuid.uuid4()),
                "decision_id": random_decision_id,
                "accepted": 0,
                "reason": "TEST_NO_STRATEGY_DECISION",
                "mode": "PAPER",
                "source": "radar",
                "gate_reason": "TEST",
                "latency_ms": 10.0,
                "posterior": 0.0,
                "p_adj": 0.0,
                "qty": 0.0,
                "ev_net": 0.0,
                "avg_entry_price": 0.0,
                "created_ts_utc": "2026-04-26T00:00:00+00:00",
                "market_id": "m1",
                "asset_id": "a1",
                "market_tier": "t3",
            })
            row = db.conn.execute(
                "SELECT decision_id FROM execution_records WHERE reason=?",
                ("TEST_NO_STRATEGY_DECISION",)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], random_decision_id)
            db.close()

