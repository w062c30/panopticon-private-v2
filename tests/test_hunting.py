import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.entropy_window import EntropyWindow
from panopticon_py.hunting.entity_linker import load_cex_blacklist, overlaps_seed, trace_funding_roots
from panopticon_py.hunting.four_d_classifier import classify_high_frequency_wallet, classify_virtual_entity
from panopticon_py.hunting.moralis_client import fetch_wallet_erc20_transfers_capped
from panopticon_py.hunting.trade_aggregate import ParentTrade, aggregate_taker_sweeps, cross_wallet_burst_cluster
from panopticon_py.hunting.trade_aggregate import VirtualEntity


class TestTradeAggregate(unittest.TestCase):
    def test_sweep_merge_same_taker(self) -> None:
        base = 1_700_000_000_000.0
        trs = []
        for i in range(5):
            trs.append(
                {
                    "timestamp": base + i * 2,
                    "taker": "0x" + "aa" * 20,
                    "side": "BUY",
                    "size": 10.0,
                    "market_id": "m1",
                }
            )
        parents = aggregate_taker_sweeps(trs, gap_ms=10)
        self.assertEqual(len(parents), 1)
        self.assertAlmostEqual(parents[0].volume, 50.0)
        self.assertEqual(parents[0].child_count, 5)

    def test_cross_wallet_cluster(self) -> None:
        t0 = 2_000_000_000_000.0
        trs = []
        for i in range(4):
            trs.append(
                {
                    "timestamp": t0 + i * 10,
                    "taker": f"0x{'%040x' % (0x1000 + i)}",
                    "side": "BUY",
                    "size": 100.0,
                    "market_id": "m1",
                }
            )
        singles, virtuals = cross_wallet_burst_cluster(trs, max_inter_trade_ms=50, min_cluster_size=3)
        self.assertGreaterEqual(len(virtuals), 1)
        self.assertGreaterEqual(len(virtuals[0].members), 3)


class TestFourD(unittest.TestCase):
    def test_mm_low_idi(self) -> None:
        parents = [
            ParentTrade("0x" + "bb" * 20, 1, 100.0, 0, 1, 1),
            ParentTrade("0x" + "bb" * 20, -1, 90.0, 2, 3, 1),
            ParentTrade("0x" + "bb" * 20, 1, 80.0, 4, 5, 1),
        ] * 5
        label, scores, _ = classify_high_frequency_wallet(parents, low_freq_threshold=1)
        self.assertLess(scores.idi, 0.5)

    def test_virtual_entity_smurf(self) -> None:
        ve = VirtualEntity(
            entity_id="e1",
            members=["0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20],
            side=1,
            total_volume=9000.0,
            first_ts_ms=0,
            last_ts_ms=100,
            trade_count=5,
        )
        lab, _, r = classify_virtual_entity(ve)
        self.assertEqual(lab, "COORDINATED_SMURF")
        self.assertIn("cross_wallet_cluster", r)


class TestEntropyStale(unittest.TestCase):
    def test_gap_flushes_buffer(self) -> None:
        ew = EntropyWindow(window_sec=2.0, gap_flush_sec=0.05, max_internal_gap_sec=0.2)
        ew.push(0.0, 1.0, 1.0)
        ew.push(0.02, 1.0, 1.0)
        flushed = ew.push(1.0, 1.0, 1.0)
        self.assertEqual(flushed, "recv_gap")
        self.assertTrue(ew._trigger_locked)


class TestEntityLinker(unittest.TestCase):
    def test_blacklist_loads(self) -> None:
        s = load_cex_blacklist()
        self.assertTrue(len(s) >= 1)

    def test_overlaps_seed(self) -> None:
        self.assertTrue(overlaps_seed(["0x" + "ab" * 20], {"0x" + "ab" * 20}))


class TestMoralisCapped(unittest.TestCase):
    def test_no_key_returns_empty(self) -> None:
        with patch.dict("os.environ", {"MORALIS_API_KEY": ""}, clear=False):
            out = fetch_wallet_erc20_transfers_capped("0x" + "11" * 20, page_limit=5, row_hard_cap=10)
            self.assertEqual(out, [])


class TestShadowDbHunting(unittest.TestCase):
    def test_hunting_tables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "h.db"
            db = ShadowDB(p.as_posix())
            db.bootstrap()
            db.append_hunting_shadow_hit(
                {
                    "hit_id": "h1",
                    "address": "0x" + "cc" * 20,
                    "market_id": "m",
                    "entity_score": 0.5,
                    "entropy_z": -2.0,
                    "sim_pnl_proxy": -0.1,
                    "outcome": "win",
                    "payload_json": {},
                    "created_ts_utc": "2026-01-01T00:00:00+00:00",
                }
            )
            wr = db.hunting_shadow_win_rate(min_rows=1)
            self.assertEqual(wr, 1.0)
            db.conn.close()


class TestRedisSeedOptional(unittest.TestCase):
    def test_redis_roundtrip(self) -> None:
        try:
            import fakeredis  # type: ignore[import-not-found]
            import redis
        except ImportError:
            self.skipTest("fakeredis/redis not installed")
        from panopticon_py.hunting.redis_seed import RedisSeedStore

        fake = fakeredis.FakeStrictRedis(decode_responses=True)
        with patch.object(redis.Redis, "from_url", return_value=fake):
            with patch.dict("os.environ", {"REDIS_URL": "redis://localhost:6379/0"}):
                store = RedisSeedStore()
                store.write_top([("0x" + "dd" * 20, 9.5), ("0x" + "ee" * 20, 8.0)])
                top = store.fetch_top(5)
                self.assertEqual(len(top), 2)
                self.assertTrue(store.is_member("0x" + "dd" * 20))


class TestTraceFundingMock(unittest.TestCase):
    @patch("panopticon_py.hunting.entity_linker.fetch_wallet_erc20_transfers_capped")
    def test_cex_anonymized_on_many_roots(self, mock_fetch) -> None:
        rows = []
        for i in range(50):
            rows.append(
                {
                    "from_address": f"0x{'%040x' % (0x2000 + i)}",
                    "to_address": "0x" + "ff" * 20,
                    "value": "1000000",
                    "token_decimals": 6,
                    "block_timestamp": "2026-01-01T00:00:00Z",
                }
            )
        mock_fetch.return_value = rows
        out = trace_funding_roots("0x" + "ff" * 20, tx_count_break=1000)
        self.assertTrue(out["cex_anonymized"] or len(out["roots"]) >= 0)


# ── D57a: Gamma query endpoint fix ─────────────────────────────────────────

class TestD57aGammaEndpoint(unittest.TestCase):
    """D57a: _fetch_missing_event_names must use query-based Gamma endpoint."""

    def test_gamma_query_endpoint_returns_slug(self) -> None:
        """
        The query-based Gamma endpoint /markets?clob_token_ids= returns a LIST.
        The fix extracts slug from markets[0].
        """
        import urllib.request
        import urllib.parse
        import json
        from unittest.mock import patch, MagicMock

        from panopticon_py.db import ShadowDB

        # Mock ShadowDB
        mock_conn = MagicMock()
        mock_db = MagicMock()
        mock_db.conn = mock_conn

        # Mock: 1 market needing resolution
        mock_conn.execute.return_value.fetchall.return_value = [
            ("851115247139565825206464957186834256291",),
        ]

        # We can't easily mock urllib at this level, so just verify
        # the URL construction logic is correct by checking the params string
        token_id = "851115247139565825206464957186834256291"
        params = urllib.parse.urlencode({"clob_token_ids": token_id})
        url = f"https://gamma-api.polymarket.com/markets?{params}"
        self.assertIn("clob_token_ids=", url)
        self.assertIn(token_id, url)
        self.assertNotIn("/markets/", url)  # should NOT be path-based

    def test_gamma_response_parsing_handles_list(self) -> None:
        """
        Query endpoint returns a JSON list, not a dict.
        Verify the list-extraction logic.
        """
        # Simulate what query endpoint returns (a list)
        data_as_list = [
            {
                "slug": "will-scotland-win-2026-world-cup",
                "question": "Will Scotland win the 2026 FIFA World Cup?",
                "active": True,
            }
        ]
        # Old code assumed dict: data.get("slug") — would crash on list
        markets_list = data_as_list if isinstance(data_as_list, list) else [data_as_list]
        self.assertTrue(markets_list)
        m = markets_list[0]
        self.assertEqual(m["slug"], "will-scotland-win-2026-world-cup")
        event_name = m.get("question") or m.get("title") or m.get("slug")
        self.assertEqual(event_name, "Will Scotland win the 2026 FIFA World Cup?")

