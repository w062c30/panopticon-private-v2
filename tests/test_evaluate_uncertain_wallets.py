import tempfile
import unittest
from pathlib import Path

from panopticon_py.db import ShadowDB
from scripts.evaluate_uncertain_wallets import run


class TestEvaluateUncertainWallets(unittest.TestCase):
    def test_promote_and_evict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "panopticon.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            db.upsert_watched_wallet(
                {
                    "address": "0x" + "11" * 20,
                    "label": "WATCHLIST_UNCERTAIN",
                    "source": "test",
                    "created_ts_utc": "2026-01-01T00:00:00+00:00",
                    "active": 1,
                }
            )
            db.upsert_watched_wallet(
                {
                    "address": "0x" + "22" * 20,
                    "label": "WATCHLIST_UNCERTAIN",
                    "source": "test",
                    "created_ts_utc": "2026-01-01T00:00:00+00:00",
                    "active": 1,
                }
            )
            for i in range(5):
                db.append_paper_trade(
                    {
                        "paper_trade_id": f"p-win-{i}",
                        "decision_id": f"d-win-{i}",
                        "wallet_address": "0x" + "11" * 20,
                        "market_id": "m1",
                        "cluster_id": "c1",
                        "side": "YES",
                        "sizing_notional": 10.0,
                        "kelly_fraction": 0.1,
                        "cluster_delta_before": 0.0,
                        "cluster_delta_after": 1.0,
                        "reason": "DRY_RUN_PAPER_EXECUTED",
                        "outcome": "win",
                        "created_ts_utc": "2026-01-01T00:00:00+00:00",
                    }
                )
            for i in range(5):
                db.append_paper_trade(
                    {
                        "paper_trade_id": f"p-loss-{i}",
                        "decision_id": f"d-loss-{i}",
                        "wallet_address": "0x" + "22" * 20,
                        "market_id": "m1",
                        "cluster_id": "c1",
                        "side": "YES",
                        "sizing_notional": 10.0,
                        "kelly_fraction": 0.1,
                        "cluster_delta_before": 0.0,
                        "cluster_delta_after": 1.0,
                        "reason": "DRY_RUN_PAPER_EXECUTED",
                        "outcome": "loss",
                        "created_ts_utc": "2026-01-01T00:00:00+00:00",
                    }
                )
            db.close()

            rc = run(
                db_path=p.as_posix(),
                dry_run=False,
                min_trades=5,
                promote_wr=0.60,
                evict_wr=0.40,
            )
            self.assertEqual(rc, 0)

            db2 = ShadowDB(p.as_posix())
            r1 = db2.conn.execute(
                "SELECT label, active FROM watched_wallets WHERE address = ?",
                ("0x" + "11" * 20,),
            ).fetchone()
            r2 = db2.conn.execute(
                "SELECT label, active FROM watched_wallets WHERE address = ?",
                ("0x" + "22" * 20,),
            ).fetchone()
            self.assertEqual(r1[0], "SMART_MONEY_QUANT")
            self.assertEqual(int(r1[1]), 1)
            self.assertEqual(r2[0], "NOISE")
            self.assertEqual(int(r2[1]), 0)
            db2.close()

