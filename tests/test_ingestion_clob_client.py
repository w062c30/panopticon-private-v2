"""Tests for panopticon_py.ingestion.clob_client AMM detection.

D67 Q1 Ruling: Markets with spread > 0.85 are AMM markets.
AMM markets should return None from fetch_best_ask() (triggering NO_TRADE).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from panopticon_py.ingestion.clob_client import (
    AMM_SPREAD_THRESHOLD,
    fetch_best_ask,
    is_amm_market,
)


class TestIsAmmMarket:
    """Unit tests for is_amm_market() helper."""

    def test_amm_detected_btc_style(self):
        """BTC 5m style: bid=0.01, ask=0.99, spread=0.98 -> AMM."""
        assert is_amm_market(0.01, 0.99) is True

    def test_amm_detected_large_spread(self):
        """spread=0.86 > 0.85 threshold -> AMM."""
        assert is_amm_market(0.07, 0.93) is True

    def test_clob_passes_normal_spread(self):
        """Normal CLOB: bid=0.42, ask=0.44, spread=0.02 -> not AMM."""
        assert is_amm_market(0.42, 0.44) is False

    def test_clob_passes_tight_spread(self):
        """Tight spread=0.01 -> not AMM."""
        assert is_amm_market(0.49, 0.50) is False

    def test_boundary_spread_085_not_amm(self):
        """spread=0.80 < 0.85 threshold -> not AMM."""
        assert is_amm_market(0.10, 0.90) is False

    def test_none_bid_returns_false(self):
        """None bid -> not AMM (insufficient data)."""
        assert is_amm_market(None, 0.99) is False

    def test_none_ask_returns_false(self):
        """None ask -> not AMM (insufficient data)."""
        assert is_amm_market(0.01, None) is False

    def test_both_none_returns_false(self):
        assert is_amm_market(None, None) is False

    def test_threshold_constant_correct(self):
        """Verify threshold is 0.85 as per D67 Q1 ruling."""
        assert AMM_SPREAD_THRESHOLD == 0.85


class TestFetchBestAskAMM:
    """Tests for fetch_best_ask() AMM blocking behavior."""

    def test_fetch_best_ask_returns_price_for_real_clob(self):
        """Real CLOB market: bid=0.42, ask=0.44 -> returns 0.44."""
        book = {
            "bids": [{"price": "0.42", "size": "100"}],
            "asks": [{"price": "0.44", "size": "50"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            result = fetch_best_ask("TOKEN_ID_REAL_CLOB", timeout_sec=1.0)
        assert result == 0.44

    def test_fetch_best_ask_returns_none_for_amm(self):
        """AMM market: bid=0.01, ask=0.99 -> returns None (NO_TRADE)."""
        book = {
            "bids": [{"price": "0.01", "size": "1000000"}],
            "asks": [{"price": "0.99", "size": "1000000"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            result = fetch_best_ask("TOKEN_ID_AMM", timeout_sec=1.0)
        assert result is None

    def test_fetch_best_ask_returns_none_when_no_asks(self):
        """Empty asks -> returns None (existing behavior)."""
        book = {"bids": [{"price": "0.42", "size": "100"}], "asks": []}
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            result = fetch_best_ask("TOKEN_ID_NO_ASKS", timeout_sec=1.0)
        assert result is None

    def test_fetch_best_ask_returns_none_when_book_none(self):
        """fetch_book returns None -> returns None (existing behavior)."""
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=None):
            result = fetch_best_ask("TOKEN_ID_BOOK_NONE", timeout_sec=1.0)
        assert result is None

    def test_fetch_best_ask_amm_boundary_high_side(self):
        """spread=0.86 > 0.85 -> AMM blocked."""
        book = {
            "bids": [{"price": "0.07", "size": "100"}],
            "asks": [{"price": "0.93", "size": "100"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            result = fetch_best_ask("TOKEN_ID_SPREAD_086", timeout_sec=1.0)
        assert result is None

    def test_fetch_best_ask_clob_boundary_ok(self):
        """spread=0.80 < 0.85 -> returns ask price (real CLOB)."""
        book = {
            "bids": [{"price": "0.10", "size": "100"}],
            "asks": [{"price": "0.90", "size": "100"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            result = fetch_best_ask("TOKEN_ID_SPREAD_080", timeout_sec=1.0)
        assert result == 0.90

    def test_hybrid_amm_clob_returns_price(self):
        """D69 Q2: Hybrid AMM+CLOB — has_recent_clob_trades=True,
        spread=0.98 (BTC 5m style) -> returns ask price."""
        book = {
            "bids": [{"price": "0.01", "size": "1000000"}],
            "asks": [{"price": "0.99", "size": "1000000"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            with patch(
                "panopticon_py.ingestion.clob_client.has_recent_clob_trades",
                return_value=True,
            ):
                result = fetch_best_ask("TOKEN_ID_HYBRID_BTC5M", timeout_sec=1.0)
        assert result == 0.99  # Hybrid allowed through

    def test_pure_amm_no_trades_returns_none(self):
        """D69 Q2: Pure AMM — has_recent_clob_trades=False,
        spread=0.98 -> returns None (NO_TRADE)."""
        book = {
            "bids": [{"price": "0.01", "size": "1000000"}],
            "asks": [{"price": "0.99", "size": "1000000"}],
        }
        with patch("panopticon_py.ingestion.clob_client.fetch_book", return_value=book):
            with patch(
                "panopticon_py.ingestion.clob_client.has_recent_clob_trades",
                return_value=False,
            ):
                result = fetch_best_ask("TOKEN_ID_PURE_AMM", timeout_sec=1.0)
        assert result is None
