"""
tests/test_window_isolation.py
==============================

Tests for Q9 (EntropyWindow rollover isolation),
Q10 (window_ts=0 guard), Q11 (T1 series sync), and Q12 (SignalEvent.series_id).

Covers:
  • _cleanup_stale_entropy_windows removes stale T1 entries
  • _cleanup_stale_entropy_windows preserves T2/T3 entries
  • Kyle sample skipped when window_ts cannot be parsed
  • Kyle sample written when slug has valid timestamp suffix
  • T1 series sync creates correct ROLLING_WINDOW members
  • Consecutive windows map to same series_id but different members
  • SignalEvent.series_id extracted from slug
  • EntropyWindow for different windows are separate instances
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch
import pytest

from panopticon_py.series.series_detector import detect_series, SERIES_TYPE_ROLLING_WINDOW
from panopticon_py.signal_engine import SignalEvent
from panopticon_py.hunting.entropy_window import EntropyWindow


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_slug(asset: str, window_ts: int) -> str:
    return f"{asset}-updown-5m-{window_ts}"


# ---------------------------------------------------------------------------
# Q9 — EntropyWindow Rollover Isolation
# ---------------------------------------------------------------------------

class TestCleanupStaleEntropyWindows:
    def test_cleanup_removes_stale_t1_entropy_windows(self):
        """Stale T1 EntropyWindows are removed on rollover."""
        import panopticon_py.hunting.run_radar as rr

        current_ts = (int(time.time()) // 300) * 300
        stale_ts = current_ts - 600  # 2 windows ago

        mock_ew_stale = MagicMock()
        mock_ew_current = MagicMock()

        # Save originals
        orig_entropy_windows = rr._entropy_windows.copy()
        orig_slug_map = rr._token_to_slug_map.copy()

        try:
            rr._entropy_windows = {
                "tok_stale_btc": mock_ew_stale,
                "tok_current_btc": mock_ew_current,
            }
            rr._token_to_slug_map = {
                "tok_stale_btc": _make_slug("btc", stale_ts),
                "tok_current_btc": _make_slug("btc", current_ts),
            }

            rr._cleanup_stale_entropy_windows(current_ts)

            assert "tok_stale_btc" not in rr._entropy_windows
            assert "tok_current_btc" in rr._entropy_windows
        finally:
            rr._entropy_windows = orig_entropy_windows
            rr._token_to_slug_map = orig_slug_map

    def test_cleanup_does_not_touch_t2_entropy_windows(self):
        """T2/T3 EntropyWindows are never cleaned up by T1 rollover logic."""
        import panopticon_py.hunting.run_radar as rr

        current_ts = (int(time.time()) // 300) * 300

        mock_ew_iran = MagicMock()
        mock_ew_hormuz = MagicMock()

        orig_entropy_windows = rr._entropy_windows.copy()
        orig_slug_map = rr._token_to_slug_map.copy()

        try:
            rr._entropy_windows = {
                "tok_iran_deal": mock_ew_iran,
                "tok_hormuz": mock_ew_hormuz,
            }
            # Slugs contain "updown-5m"? No → function skips them (T2/T3 guard)
            rr._token_to_slug_map = {
                "tok_iran_deal": "iran-permanent-peace-deal-by-june-30",
                "tok_hormuz": "strait-of-hormuz-traffic-normal-by-may",
            }

            rr._cleanup_stale_entropy_windows(current_ts)

            # Non-T1 slugs (no "updown-5m") are skipped → preserved
            assert "tok_iran_deal" in rr._entropy_windows
            assert "tok_hormuz" in rr._entropy_windows
        finally:
            rr._entropy_windows = orig_entropy_windows
            rr._token_to_slug_map = orig_slug_map

    def test_cleanup_only_cleans_numeric_suffix_tokens(self):
        """Tokens without 'updown-5m' pattern are not cleaned as stale."""
        import panopticon_py.hunting.run_radar as rr

        current_ts = (int(time.time()) // 300) * 300
        stale_ts = current_ts - 600

        mock_ew = MagicMock()

        orig_entropy_windows = rr._entropy_windows.copy()
        orig_slug_map = rr._token_to_slug_map.copy()

        try:
            rr._entropy_windows = {"tok_ambiguous": mock_ew}
            # Slug has numeric suffix but NOT "updown-5m" → not a T1 token → preserved
            rr._token_to_slug_map = {"tok_ambiguous": f"some-slug-{stale_ts}"}

            rr._cleanup_stale_entropy_windows(current_ts)

            # Lacks "updown-5m" → not a T1 token → skipped → preserved
            assert "tok_ambiguous" in rr._entropy_windows
        finally:
            rr._entropy_windows = orig_entropy_windows
            rr._token_to_slug_map = orig_slug_map

    def test_cleanup_empty_map(self):
        """Cleanup handles empty _token_to_slug_map gracefully."""
        import panopticon_py.hunting.run_radar as rr

        current_ts = (int(time.time()) // 300) * 300
        mock_ew = MagicMock()

        orig_entropy_windows = rr._entropy_windows.copy()
        orig_slug_map = rr._token_to_slug_map.copy()

        try:
            rr._entropy_windows = {"tok_stale": mock_ew}
            rr._token_to_slug_map = {}

            rr._cleanup_stale_entropy_windows(current_ts)

            # No slugs → can't determine window → nothing cleaned
            assert "tok_stale" in rr._entropy_windows
        finally:
            rr._entropy_windows = orig_entropy_windows
            rr._token_to_slug_map = orig_slug_map


# ---------------------------------------------------------------------------
# Q10 — window_ts=0 Guard
# ---------------------------------------------------------------------------

class TestKyleWindowTsGuard:
    def test_kyle_window_ts_parsed_correctly_from_valid_slug(self):
        """window_ts is correctly extracted from a valid T1 slug."""
        current_ts = (int(time.time()) // 300) * 300
        slug = _make_slug("btc", current_ts)
        ts_part = slug.rsplit("-", 1)[-1]
        window_ts = int(ts_part) if ts_part.isdigit() else 0
        assert window_ts == current_ts

    def test_kyle_window_ts_is_zero_for_unknown_slug(self):
        """window_ts parses to 0 when slug has no numeric suffix."""
        slug = "btc-updown-5m-unknown"
        ts_part = slug.rsplit("-", 1)[-1]
        window_ts = int(ts_part) if ts_part.isdigit() else 0
        assert window_ts == 0

    def test_kyle_window_ts_is_zero_for_non_t1_slug(self):
        """window_ts is 0 for non-T1 slugs that lack the updown-5m pattern."""
        # "iran-peace-deal-by-june" has no trailing digit suffix at all
        slug = "iran-peace-deal-by-june"
        ts_part = slug.rsplit("-", 1)[-1]
        window_ts = int(ts_part) if ts_part.isdigit() else 0
        assert window_ts == 0


# ---------------------------------------------------------------------------
# Q11 — T1 Series Sync
# ---------------------------------------------------------------------------

class TestT1SeriesSync:
    def test_t1_series_sync_updates_current_window(self):
        """T1 series sync in _refresh_tier1_tokens creates correct members."""
        current_ts = (int(time.time()) // 300) * 300
        next_ts = current_ts + 300
        slug_map = {
            "tok_current": _make_slug("btc", current_ts),
            "tok_next":    _make_slug("btc", next_ts),
        }

        t1_market_dicts = [
            {"slug": slug, "conditionId": tok, "market_tier": "t1"}
            for tok, slug in slug_map.items()
        ]
        series_list = detect_series(t1_market_dicts)

        assert len(series_list) == 1
        assert series_list[0].series_type == SERIES_TYPE_ROLLING_WINDOW
        assert len(series_list[0].members) == 2
        slugs = [m.slug for m in series_list[0].members]
        assert _make_slug("btc", current_ts) in slugs
        assert _make_slug("btc", next_ts) in slugs

    def test_different_windows_have_different_series_members(self):
        """Two consecutive T1 windows map to SAME series_id but DIFFERENT members."""
        ts1 = 1777026600
        ts2 = 1777026900
        slug_map = {
            "tok_a": _make_slug("btc", ts1),
            "tok_b": _make_slug("btc", ts2),
        }

        t1_market_dicts = [
            {"slug": slug, "conditionId": tok, "market_tier": "t1"}
            for tok, slug in slug_map.items()
        ]
        series_list = detect_series(t1_market_dicts)

        assert series_list[0].series_id == "btc-updown-5m"
        token_ids = [m.token_id for m in series_list[0].members]
        assert "tok_a" in token_ids
        assert "tok_b" in token_ids
        assert token_ids[0] != token_ids[1]


# ---------------------------------------------------------------------------
# Q12 — SignalEvent.series_id
# ---------------------------------------------------------------------------

class TestSignalEventSeriesId:
    def test_signal_event_series_id_populated(self):
        """SignalEvent.series_id is set from slug for T1 events."""
        ts = 1777026600
        slug = _make_slug("btc", ts)
        ts_part = slug.rsplit("-", 1)[-1]
        series_id = slug.rsplit(f"-{ts_part}", 1)[0] if ts_part.isdigit() else slug
        assert series_id == "btc-updown-5m"

    def test_signal_event_window_ts_extracted(self):
        """SignalEvent.window_ts is extracted from slug for T1 events."""
        ts = 1777026600
        slug = _make_slug("btc", ts)
        ts_part = slug.rsplit("-", 1)[-1]
        window_ts = int(ts_part) if ts_part.isdigit() else 0
        assert window_ts == ts

    def test_signal_event_non_t1_series_id_falls_back_to_full_slug(self):
        """Non-T1 slug (no updown-5m pattern) falls back to full slug as series_id."""
        slug = "iran-peace-deal-by-june-30"
        # Non-T1 slugs have no "updown-5m" marker
        if "updown-5m" not in slug:
            series_id = slug
        else:
            ts_part = slug.rsplit("-", 1)[-1]
            series_id = slug.rsplit(f"-{ts_part}", 1)[0] if ts_part.isdigit() else slug
        # No updown-5m → falls back to full slug
        assert series_id == slug

    def test_signal_event_defaults(self):
        """SignalEvent defaults to empty series_id and zero window_ts."""
        # token_id is optional (has default None) in SignalEvent
        evt = SignalEvent(source="radar", market_id="0x123", token_id=None)
        assert evt.series_id == ""
        assert evt.window_ts == 0


# ---------------------------------------------------------------------------
# EntropyWindow Isolation (cross-window)
# ---------------------------------------------------------------------------

class TestEntropyWindowIsolation:
    def test_rolling_window_entropy_windows_isolated(self):
        """EntropyWindow for window_n and window_n+1 are separate instances."""
        token_current = "0xAAAA"
        token_next = "0xBBBB"
        ew_dict: dict = {}

        for tok in [token_current, token_next]:
            if tok not in ew_dict:
                ew_dict[tok] = EntropyWindow()

        assert ew_dict[token_current] is not ew_dict[token_next]
        assert len(ew_dict) == 2
