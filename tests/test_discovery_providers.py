import asyncio
import unittest
from unittest.mock import patch

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.discovery_loop import (
    _extract_wallet_candidates_from_gamma_payload,
    make_hybrid_history_fetcher,
    with_retry,
)


class DiscoveryProviderTests(unittest.TestCase):
    def test_extract_gamma_candidates(self) -> None:
        payload = {
            "data": [
                {"wallet": "0x" + "1" * 40, "market_id": "m1", "win_rate": 0.8, "pnl": 100},
                {"wallet": "0x" + "1" * 40, "market_id": "m2", "win_rate": 0.9, "pnl": 120},
                {"wallet": "0x" + "1" * 40, "market_id": "m3", "win_rate": 0.85, "pnl": 80},
                {"wallet": "0x" + "1" * 40, "market_id": "m4", "win_rate": 0.75, "pnl": 20},
                {"wallet": "0x" + "1" * 40, "market_id": "m5", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m6", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m7", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m8", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m9", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m10", "win_rate": 0.7, "pnl": 50},
                {"wallet": "0x" + "1" * 40, "market_id": "m11", "win_rate": 0.7, "pnl": 50},
            ]
        }
        out = _extract_wallet_candidates_from_gamma_payload(
            payload,
            min_markets=10,
            min_win_rate=0.6,
            limit=500,
        )
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].wallet_address.startswith("0x"))

    def test_with_retry_eventual_success(self) -> None:
        state = {"n": 0}

        async def fn() -> int:
            state["n"] += 1
            if state["n"] < 3:
                raise RuntimeError("retry")
            return 7

        got = asyncio.run(with_retry(fn, retries=4, base_backoff_sec=0.01))
        self.assertEqual(got, 7)

    def test_hybrid_prefers_observation(self) -> None:
        db = ShadowDB(":memory:")
        db.bootstrap()
        for i in range(25):
            db.append_wallet_observation(
                {
                    "obs_id": f"o{i}",
                    "address": "0x" + "a" * 40,
                    "market_id": "m1",
                    "obs_type": "clob_trade",
                    "payload_json": {"trade": {"side": "BUY", "size": 12}},
                    "ingest_ts_utc": f"2026-04-21T00:00:{i:02d}Z",
                }
            )

        async def _run() -> None:
            with patch("panopticon_py.hunting.discovery_loop.fetch_wallet_erc20_transfers_capped") as moralis_fetch:
                fetcher = await make_hybrid_history_fetcher(db)
                rows = await fetcher("0x" + "a" * 40)
                self.assertGreaterEqual(len(rows), 20)
                moralis_fetch.assert_not_called()

        asyncio.run(_run())
        db.close()

    def test_hybrid_backfills_moralis_when_obs_insufficient(self) -> None:
        db = ShadowDB(":memory:")
        db.bootstrap()

        async def _run() -> None:
            with patch(
                "panopticon_py.hunting.discovery_loop.fetch_wallet_erc20_transfers_capped",
                return_value=[
                    {
                        "from_address": "0x" + "b" * 40,
                        "to_address": "0x" + "c" * 40,
                        "value": "10",
                        "block_timestamp": "2026-04-21T00:00:00Z",
                        "token_address": "0x" + "d" * 40,
                    }
                ],
            ) as moralis_fetch:
                fetcher = await make_hybrid_history_fetcher(db)
                rows = await fetcher("0x" + "b" * 40)
                self.assertGreaterEqual(len(rows), 1)
                moralis_fetch.assert_called_once()

        asyncio.run(_run())
        db.close()


if __name__ == "__main__":
    unittest.main()
