"""
tests/test_t1_market_clock.py

Tests for the T1 Market Clock module (panopticon_py/hunting/t1_market_clock.py).
The T1 Market Clock computes BTC/ETH/SOL 5-min Up-or-Down market slugs and token IDs
deterministically from Unix timestamps, without needing the Gamma listing API.

Confirmed slug patterns (from live diagnostic):
  btc-updown-5m-{unix_ts}  → "Bitcoin Up or Down - April 24, 2:05AM-2:10AM ET"
  eth-updown-5m-{unix_ts}  → "Ethereum Up or Down - April 24, 2:05AM-2:10AM ET"
  sol-updown-5m-{unix_ts}  → "Solana Up or Down - April 24, 2:05AM-2:10AM ET"
"""

from __future__ import annotations

import pytest

from panopticon_py.hunting.t1_market_clock import (
    compute_t1_slugs,
    get_current_t1_window,
    is_t1_window_boundary,
    get_window_expiry_seconds,
    _extract_token_ids,
)


class TestComputeT1Slugs:
    """Test slug computation from Unix timestamps."""

    def test_btc_eth_sol_all_present(self) -> None:
        """All three asset prefixes appear for each window."""
        slugs = compute_t1_slugs(window_ts=1777010700, prefetch=2)
        assert "btc-updown-5m-1777010700" in slugs
        assert "eth-updown-5m-1777010700" in slugs
        assert "sol-updown-5m-1777010700" in slugs

    def test_next_window_slugs_present(self) -> None:
        """Prefetch=2 includes the next 5-min window (ts + 300)."""
        slugs = compute_t1_slugs(window_ts=1777010700, prefetch=2)
        assert "btc-updown-5m-1777011000" in slugs  # +300s = next window
        assert "eth-updown-5m-1777011000" in slugs
        assert "sol-updown-5m-1777011000" in slugs

    def test_window_count_matches_prefetch(self) -> None:
        """Prefetch=3 gives 3 windows × 3 assets = 9 slugs."""
        slugs = compute_t1_slugs(window_ts=1777010700, prefetch=3)
        assert len(slugs) == 9

    def test_default_prefetch_is_5(self) -> None:
        """Default prefetch produces 5 windows × 3 assets = 15 slugs."""
        slugs = compute_t1_slugs(window_ts=1777010700)
        assert len(slugs) == 15

    def test_slugs_are_unique(self) -> None:
        """No duplicate slugs in output."""
        slugs = compute_t1_slugs(window_ts=1777010700, prefetch=3)
        assert len(slugs) == len(set(slugs))


class TestGetCurrentT1Window:
    """Test that window timestamps are always aligned to 5-min boundaries."""

    def test_alignment_to_300_seconds(self) -> None:
        """Window timestamp must be divisible by 300 (5 min)."""
        ts = get_current_t1_window()
        assert ts % 300 == 0

    def test_alignment_is_stable_within_same_window(self) -> None:
        """Multiple calls within the same 5-min window return the same timestamp."""
        import time
        ts1 = get_current_t1_window()
        time.sleep(0.01)
        ts2 = get_current_t1_window()
        assert ts1 == ts2


class TestIsT1WindowBoundary:
    """Test the window boundary detection trigger."""

    def test_boundary_detected_in_last_30_seconds(self) -> None:
        """is_t1_window_boundary returns True in the last 30 seconds of a window."""
        import unittest.mock
        import panopticon_py.hunting.t1_market_clock as t1c

        # 280s into window (1777010700 + 280) = 20s before expiry → boundary
        with unittest.mock.patch.object(t1c._time, "time", return_value=float(1777010700 + 280)):
            result = is_t1_window_boundary(threshold_secs=30)
            assert result is True, "Should detect boundary in last 30s"

    def test_not_triggered_early_in_window(self) -> None:
        """Should return False early in the window (e.g., 10s in)."""
        import unittest.mock
        import panopticon_py.hunting.t1_market_clock as t1c

        # 10s into window → NOT a boundary
        with unittest.mock.patch.object(t1c._time, "time", return_value=float(1777010700 + 10)):
            result = is_t1_window_boundary(threshold_secs=30)
            assert result is False, "Should not trigger early in window"


class TestExtractTokenIds:
    """Test the _extract_token_ids helper across all input formats."""

    def test_string_json_input(self) -> None:
        """JSON string of token list should parse correctly."""
        m = {"clobTokenIds": '["abc123", "def456"]'}
        result = _extract_token_ids(m)
        assert result == ["abc123", "def456"]

    def test_list_input(self) -> None:
        """Already-parsed list should pass through."""
        m = {"clobTokenIds": ["abc123", "def456"]}
        result = _extract_token_ids(m)
        assert result == ["abc123", "def456"]

    def test_nested_list_input(self) -> None:
        """Nested list [[token1, token2]] should flatten."""
        m = {"clobTokenIds": [["abc123", "def456"]]}
        result = _extract_token_ids(m)
        assert result == ["abc123", "def456"]

    def test_empty_list(self) -> None:
        """Empty list returns empty result."""
        m = {"clobTokenIds": []}
        result = _extract_token_ids(m)
        assert result == []

    def test_missing_key(self) -> None:
        """Missing clobTokenIds key returns empty list."""
        m = {}
        result = _extract_token_ids(m)
        assert result == []

    def test_null_value(self) -> None:
        """None value returns empty list."""
        m = {"clobTokenIds": None}
        result = _extract_token_ids(m)
        assert result == []

    def test_invalid_json_string(self) -> None:
        """Malformed JSON string returns empty list (no crash)."""
        m = {"clobTokenIds": "not valid json {"}
        result = _extract_token_ids(m)
        assert result == []

    def test_mixed_nested_flat(self) -> None:
        """Mixed nested list with some flat items."""
        m = {"clobTokenIds": ["token1", ["token2", "token3"]]}
        result = _extract_token_ids(m)
        assert result == ["token1", "token2", "token3"]


class TestGetWindowExpirySeconds:
    """Test seconds-remaining computation."""

    def test_returns_value_in_valid_range(self) -> None:
        """Must return an integer between 0 and 299 inclusive."""
        secs = get_window_expiry_seconds()
        assert isinstance(secs, int)
        assert 0 <= secs <= 299

    def test_get_window_expiry_returns_reasonable_range(self) -> None:
        """Window expiry seconds should always be in [0, 299]."""
        import unittest.mock
        import panopticon_py.hunting.t1_market_clock as t1c

        # Patch t1c._time.time directly since get_window_expiry_seconds
        # calls get_corrected_unix_time() which uses _time.time()
        for test_ts in [1777010700.0, 1777010700.0 + 150.0, 1777010700.0 + 299.0]:
            with unittest.mock.patch.object(t1c._time, "time", return_value=test_ts):
                secs = get_window_expiry_seconds()
                assert 0 <= secs <= 299, f"ts={test_ts} gave secs={secs}"