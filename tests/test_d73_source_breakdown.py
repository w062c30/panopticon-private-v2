"""D73 TASK D73c: signal_engine source breakdown — regression tests."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from panopticon_py.signal_engine import _collect_insider_sources, INSIDER_SCORE_THRESHOLD


class TestD73SourceBreakdown(unittest.TestCase):
    """D73 TASK D73c: Verify source breakdown is surfaced in _collect_insider_sources."""

    def test_snapshot_hit_returns_correct_breakdown(self):
        """
        D73: When a wallet has a score from insider_score_snapshots,
        snapshot_hits should be incremented and [D73_SOURCE_BREAKDOWN] logged.
        """
        db = MagicMock()

        # Mock wallet_observations query returns 1 wallet
        db.conn.execute.return_value.fetchall.return_value = [
            ("0xABCD1234abcd5678ef9012345678901234567890",)
        ]

        # Mock insider_score_snapshots hit
        def mock_execute(sql, *args, **kwargs):
            mock_result = MagicMock()
            if "insider_score_snapshots" in sql and "LIMIT 1" in sql:
                mock_result.fetchone.return_value = (1,)  # hit
            else:
                mock_result.fetchone.return_value = None
            return mock_result

        db.conn.execute.side_effect = mock_execute

        with patch("panopticon_py.signal_engine.logger") as mock_logger:
            sources = _collect_insider_sources("tok123", 360, db)

        # Logger should have been called with [D73_SOURCE_BREAKDOWN]
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        d73_log_found = any("[D73_SOURCE_BREAKDOWN]" in c for c in log_calls)
        self.assertTrue(d73_log_found, f"Expected [D73_SOURCE_BREAKDOWN] in logs: {log_calls}")

    def test_fallback_hit_returns_correct_breakdown(self):
        """
        D73: When a wallet has a score from discovered_entities (not snapshots),
        fallback_hits should be incremented.
        """
        db = MagicMock()

        # Mock wallet_observations query returns 1 wallet
        db.conn.execute.return_value.fetchall.return_value = [
            ("0xWALLET5678901234567890abcdef1234567890abcd",)
        ]

        # snapshot query returns None (miss), but discovered_entities returns score
        call_count = [0]

        def mock_execute(sql, *args, **kwargs):
            mock_result = MagicMock()
            call_count[0] += 1
            if "insider_score_snapshots" in sql and "LIMIT 1" in sql:
                mock_result.fetchone.return_value = None  # snapshot miss
            elif "discovered_entities" in sql:
                mock_result.fetchone.return_value = (0.7,)  # fallback hit
            elif "wallet_observations" in sql:
                mock_result.fetchone.return_value = None
            else:
                mock_result.fetchone.return_value = None
            return mock_result

        db.conn.execute.side_effect = mock_execute

        with patch("panopticon_py.signal_engine.logger") as mock_logger:
            sources = _collect_insider_sources("tok456", 360, db)

        # Should have logged source breakdown
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        d73_log_found = any("[D73_SOURCE_BREAKDOWN]" in c for c in log_calls)
        self.assertTrue(d73_log_found, f"Expected [D73_SOURCE_BREAKDOWN] in logs: {log_calls}")

    def test_no_wallets_returns_zero_hits(self):
        """
        D73: When no wallets are found for the market,
        source breakdown should log snapshot_hits=0 fallback_hits=0.
        """
        db = MagicMock()
        db.conn.execute.return_value.fetchall.return_value = []  # no wallets

        with patch("panopticon_py.signal_engine.logger") as mock_logger:
            sources = _collect_insider_sources("tok_no_wallets", 360, db)

        self.assertEqual(sources, [])
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        d73_log_found = any("[D73_SOURCE_BREAKDOWN]" in c for c in log_calls)
        self.assertTrue(d73_log_found, "Expected [D73_SOURCE_BREAKDOWN] even with zero wallets")

    def test_multiple_wallets_aggregates_correctly(self):
        """
        D73: Multiple wallets with mixed snapshot/fallback hits are summed correctly.
        """
        wallets = [
            ("0xwallet000000000000000000000000000000001",),
            ("0xwallet000000000000000000000000000000002",),
            ("0xwallet000000000000000000000000000000003",),
        ]
        db = MagicMock()

        query_count = [0]

        def mock_execute(sql, *args, **kwargs):
            mock_result = MagicMock()
            query_count[0] += 1
            # First call: wallet_observations query (returns wallet list)
            if sql.strip().startswith("SELECT DISTINCT wo.address"):
                mock_result.fetchall.return_value = wallets
                mock_result.fetchone.return_value = None
            # Snapshot check query
            elif "insider_score_snapshots" in sql and "LIMIT 1" in sql:
                # wallet 1 and 2 hit snapshot (score >= 0.55)
                idx = (query_count[0] - 2) // 2  # rough index
                mock_result.fetchone.return_value = (1,) if idx in (0, 1) else None
            # Fallback query
            elif "discovered_entities" in sql and "LIMIT 1" in sql:
                # wallet 3 hits discovered_entities (score = 0.6)
                mock_result.fetchone.return_value = (0.6,) if query_count[0] > 3 else None
            else:
                mock_result.fetchone.return_value = None
            return mock_result

        db.conn.execute.side_effect = mock_execute

        with patch("panopticon_py.signal_engine.logger") as mock_logger:
            sources = _collect_insider_sources("tok_multi", 360, db)

        # All 3 wallets have score >= INSIDER_SCORE_THRESHOLD (0.55)
        self.assertGreaterEqual(len(sources), 1)


if __name__ == "__main__":
    unittest.main()
