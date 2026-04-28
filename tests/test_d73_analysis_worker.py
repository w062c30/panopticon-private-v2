"""D73 analysis_worker gated fix + source breakdown — regression tests."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from panopticon_py.ingestion.analysis_worker import InsiderAnalysisWorker
from panopticon_py.ingestion.wallet_features import WalletAggFeatures


class TestD73GatedFix(unittest.TestCase):
    """D73 TASK D73c: Verify skip gate behavior for clob_trade vs non-clob_trade wallets."""

    def _make_worker(self) -> InsiderAnalysisWorker:
        """Create a worker with mocked db and writer."""
        db = MagicMock()
        writer = MagicMock()
        return InsiderAnalysisWorker(db, writer)

    def test_single_clob_trade_wallet_not_skipped(self):
        """
        D73: A wallet with exactly 1 clob_trade observation should NOT be skipped
        by the relaxed gate (trade_count < 1 and score < 0.06 is the skip condition).
        Since trade_count == 1, trade_count < 1 is False → wallet passes.
        """
        worker = self._make_worker()
        obs = [
            {
                "obs_id": "obs1",
                "address": "0xtest",
                "market_id": "tok1",
                "obs_type": "clob_trade",
                "payload": {"side": "BUY", "price": 0.6, "size": 10.0},
                "ingest_ts_utc": "2026-04-29T00:00:00Z",
            }
        ]
        feats = WalletAggFeatures(trade_count=1, volume_proxy=6.0, unique_markets=1, burst_score=0.04)
        score = 0.3  # below 0.06 — old gate would skip

        should_skip, reason = worker._should_skip_wallet(obs, feats, score)

        self.assertFalse(should_skip, "Single clob_trade wallet should NOT be skipped under D73 gate")

    def test_single_clob_trade_zero_trade_count_skipped(self):
        """
        D73: A wallet with 0 trades and score < 0.06 should STILL be skipped,
        even if it has a clob_trade observation in the raw obs list.
        """
        worker = self._make_worker()
        # Obs has clob_trade but aggregate_from_observations returns trade_count=0
        # (edge case: malformed payload)
        obs = [
            {
                "obs_id": "obs1",
                "address": "0xtest",
                "market_id": "tok1",
                "obs_type": "clob_trade",
                "payload": {},  # empty — no size/price → trade_count stays 0
                "ingest_ts_utc": "2026-04-29T00:00:00Z",
            }
        ]
        feats = WalletAggFeatures(trade_count=0, volume_proxy=0.0, unique_markets=0, burst_score=0.0)
        score = 0.05  # below 0.06

        should_skip, reason = worker._should_skip_wallet(obs, feats, score)

        self.assertTrue(should_skip, "Wallet with 0 trades and score < 0.06 should be skipped")
        self.assertEqual(reason, "low_score")

    def test_non_clob_trade_single_trade_still_skipped(self):
        """
        D73: A wallet with only non-clob_trade observations should still follow
        the original gate (trade_count < 2 and score < 0.06 skips).
        """
        worker = self._make_worker()
        obs = [
            {
                "obs_id": "obs1",
                "address": "0xtest2",
                "market_id": "tok1",
                "obs_type": "other_type",
                "payload": {},
                "ingest_ts_utc": "2026-04-29T00:00:00Z",
            }
        ]
        feats = WalletAggFeatures(trade_count=0, volume_proxy=0.0, unique_markets=0, burst_score=0.0)
        score = 0.05

        should_skip, reason = worker._should_skip_wallet(obs, feats, score)

        self.assertTrue(should_skip, "Non-clob_trade wallet with trade_count=0 should still be skipped")
        self.assertEqual(reason, "trade_count")

    def test_non_clob_trade_two_trades_not_skipped(self):
        """
        D73: A wallet with 2+ non-clob_trade trades and score >= 0.06 should NOT be skipped.
        """
        worker = self._make_worker()
        obs = [
            {
                "obs_id": f"obs{i}",
                "address": "0xtest3",
                "market_id": "tok1",
                "obs_type": "other_type",
                "payload": {},
                "ingest_ts_utc": "2026-04-29T00:00:00Z",
            }
            for i in range(3)
        ]
        feats = WalletAggFeatures(trade_count=2, volume_proxy=0.0, unique_markets=0, burst_score=0.08)
        score = 0.08  # >= 0.06

        should_skip, reason = worker._should_skip_wallet(obs, feats, score)

        self.assertFalse(should_skip, "Non-clob_trade wallet with 2+ trades and score >= 0.06 should pass")

    def test_clob_trade_high_score_not_skipped(self):
        """
        D73: A clob_trade wallet with high score (>= 0.06) should NOT be skipped,
        regardless of trade_count.
        """
        worker = self._make_worker()
        obs = [
            {
                "obs_id": "obs1",
                "address": "0xtest4",
                "market_id": "tok1",
                "obs_type": "clob_trade",
                "payload": {"side": "BUY", "price": 0.8, "size": 5.0},
                "ingest_ts_utc": "2026-04-29T00:00:00Z",
            }
        ]
        feats = WalletAggFeatures(trade_count=1, volume_proxy=4.0, unique_markets=1, burst_score=0.04)
        score = 0.7  # high score >= 0.06

        should_skip, reason = worker._should_skip_wallet(obs, feats, score)

        self.assertFalse(should_skip, "clob_trade wallet with score >= 0.06 should not be skipped")


class TestD73AnalysisWorkerTick(unittest.TestCase):
    """D73: Verify _tick emits D73_ANALYSIS_TICK and D73_ANALYSIS_SKIP logs."""

    @patch("panopticon_py.ingestion.analysis_worker.logger")
    def test_tick_emits_d73_t0_log(self, mock_logger):
        """Tick should emit [D73_ANALYSIS_TICK] with wallet count."""
        db = MagicMock()
        db.fetch_distinct_trade_wallets.return_value = ["0xwallet1", "0xwallet2"]
        db.fetch_recent_wallet_observations.return_value = []
        db.conn.execute.return_value.fetchone.return_value = (5,)

        writer = MagicMock()
        worker = InsiderAnalysisWorker(db, writer)
        worker._tick()

        # Check that D73_ANALYSIS_TICK was logged
        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        d73_tick_found = any("[D73_ANALYSIS_TICK]" in c for c in log_calls)
        self.assertTrue(d73_tick_found, f"Expected [D73_ANALYSIS_TICK] in logs: {log_calls}")

    @patch("panopticon_py.ingestion.analysis_worker.logger")
    def test_tick_emits_d73_skip_log(self, mock_logger):
        """Tick should emit [D73_ANALYSIS_SKIP] with skip breakdown."""
        db = MagicMock()
        db.fetch_distinct_trade_wallets.return_value = ["0xwallet1"]
        # Empty observations → trade_count=0 → skipped
        db.fetch_recent_wallet_observations.return_value = []
        db.conn.execute.return_value.fetchone.return_value = (3,)

        writer = MagicMock()
        worker = InsiderAnalysisWorker(db, writer)
        worker._tick()

        log_calls = [str(c) for c in mock_logger.info.call_args_list]
        d73_skip_found = any("[D73_ANALYSIS_SKIP]" in c for c in log_calls)
        self.assertTrue(d73_skip_found, f"Expected [D73_ANALYSIS_SKIP] in logs: {log_calls}")


if __name__ == "__main__":
    unittest.main()
