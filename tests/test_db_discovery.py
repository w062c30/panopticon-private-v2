import tempfile
import unittest
from pathlib import Path

from panopticon_py.db import ShadowDB


class DBDiscoveryTests(unittest.TestCase):
    def test_upsert_discovered_entity_and_wallet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "panopticon.db"
            db = ShadowDB(dbp.as_posix())
            db.bootstrap()
            db.upsert_discovered_entity(
                {
                    "entity_id": "ve_test",
                    "trust_score": 72.5,
                    "primary_tag": "ALGO_SLICING",
                    "sample_size": 12,
                    "last_updated_at": "2026-04-21T00:00:00Z",
                }
            )
            db.upsert_tracked_wallet(
                {
                    "wallet_address": "0x" + "1" * 40,
                    "entity_id": "ve_test",
                    "all_time_pnl": 1234.5,
                    "win_rate": 0.71,
                    "discovery_source": "macro_harvest_7d",
                    "last_updated_at": "2026-04-21T00:00:00Z",
                }
            )
            row = db.fetch_discovered_entity("ve_test")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["primary_tag"], "ALGO_SLICING")
            self.assertAlmostEqual(row["trust_score"], 72.5, places=6)
            db.close()

    def test_append_discovery_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dbp = Path(td) / "panopticon.db"
            db = ShadowDB(dbp.as_posix())
            db.bootstrap()
            db.append_discovery_audit(
                {
                    "audit_id": "a1",
                    "actor": "test",
                    "action": "DISCOVERY_CYCLE_SUMMARY",
                    "before_json": None,
                    "after_json": {"ok": True},
                    "reason": "unit",
                    "created_ts_utc": "2026-04-21T00:00:00Z",
                }
            )
            got = db.conn.execute("SELECT COUNT(*) FROM audit_log WHERE audit_id='a1'").fetchone()
            self.assertEqual(int(got[0]), 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
