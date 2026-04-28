import asyncio
import tempfile
import unittest
from pathlib import Path

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.discovery_loop import run_discovery_cycle
from panopticon_py.hunting.entity_linker import sybil_group_wallets


class DiscoveryLoopTests(unittest.TestCase):
    def test_sybil_grouping_same_roots(self) -> None:
        groups = sybil_group_wallets(
            [
                {
                    "wallet_address": "0x" + "a" * 40,
                    "funding_roots": ["0x" + "f" * 40],
                    "trade_ts_ms": [1000, 2000, 3000],
                },
                {
                    "wallet_address": "0x" + "b" * 40,
                    "funding_roots": ["0x" + "f" * 40],
                    "trade_ts_ms": [1200, 2200, 3200],
                },
            ]
        )
        members = list(groups.values())[0]
        self.assertEqual(set(members), {"0x" + "a" * 40, "0x" + "b" * 40})

    def test_discovery_cycle_hydrates_db(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as td:
                dbp = Path(td) / "panopticon.db"
                db = ShadowDB(dbp.as_posix())
                db.bootstrap()
                summary = await run_discovery_cycle(
                    db,
                    history_fetcher=lambda _w: asyncio.sleep(0, []),
                    cycle_id="cycle-test",
                )
                self.assertIn("fetched_candidates", summary)
                self.assertIn("history_from_moralis", summary)
                self.assertIn("provider_errors", summary)
                got = db.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
                self.assertGreaterEqual(int(got[0]), 1)
                db.close()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
