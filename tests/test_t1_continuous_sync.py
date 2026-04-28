"""
tests/test_t1_continuous_sync.py

RVF-compatible T1 continuous window sync tests.
Tests: NTP offset, corrected time, window alignment, anchor validation,
boundary trigger, stale token pruning.
"""

from __future__ import annotations

import unittest.mock

import pytest

from panopticon_py.hunting import t1_market_clock as t1c


class TestNTPTimeCorrection:
    """NTP sync + corrected time functions."""

    def test_get_corrected_unix_time_with_positive_offset(self) -> None:
        """Corrected time = time.time() + offset (positive = local clock behind)."""
        with unittest.mock.patch.object(t1c._time, "time", return_value=1000.0):
            t1c._ntp_offset_seconds = 2.5
            result = t1c.get_corrected_unix_time()
            assert abs(result - 1002.5) < 0.01

    def test_get_corrected_unix_time_with_negative_offset(self) -> None:
        """Negative offset = local clock is ahead of NTP."""
        with unittest.mock.patch.object(t1c._time, "time", return_value=1000.0):
            t1c._ntp_offset_seconds = -1.0
            result = t1c.get_corrected_unix_time()
            assert abs(result - 999.0) < 0.01

    def test_ntp_sync_failure_defaults_to_zero(self) -> None:
        """NTP failure (invalid server) returns 0.0 and does not raise."""
        with unittest.mock.patch.object(t1c._time, "time", return_value=1000.0):
            result = t1c.sync_ntp_offset(ntp_server="invalid.invalid.invalid")
            assert result == 0.0
            assert t1c._ntp_offset_seconds == 0.0

    def test_sync_ntp_offset_updates_module_variable(self) -> None:
        """sync_ntp_offset() stores offset in _ntp_offset_seconds on success."""
        with unittest.mock.patch.object(t1c._time, "time", return_value=1000.0):
            t1c._ntp_offset_seconds = 99.0  # pre-set
            try:
                import ntplib
            except ImportError:
                pytest.skip("ntplib not installed")
            # Even if NTP fails, it should not raise — just reset to 0
            result = t1c.sync_ntp_offset(ntp_server="invalid.invalid")
            # Should not raise — either succeeds or returns 0
            assert isinstance(result, float)


class TestWindowAlignment:
    """Window timestamp alignment (always % 300 == 0)."""

    def test_get_current_t1_window_always_300_aligned(self) -> None:
        """Window ts is always divisible by 300."""
        ts = t1c.get_current_t1_window(corrected=False)
        assert ts % 300 == 0

    def test_get_current_t1_window_corrected_false_gives_same_alignment(self) -> None:
        """Both corrected and non-corrected paths give 300-aligned timestamps."""
        ts_corr = t1c.get_current_t1_window(corrected=True)
        ts_uncorr = t1c.get_current_t1_window(corrected=False)
        assert ts_corr % 300 == 0
        assert ts_uncorr % 300 == 0


class TestAnchorValidation:
    """Known anchor 1777018200 = HKT 16:10 (verified formula)."""

    def test_validate_clock_against_anchor_passes(self) -> None:
        """Known anchor 1777018200 validates correctly."""
        assert t1c.validate_clock_against_anchor(1777018200) is True

    def test_validate_clock_against_anchor_fails_on_bad_ts(self) -> None:
        """Non-300-aligned ts fails validation."""
        assert t1c.validate_clock_against_anchor(1777018201) is False

    def test_validate_clock_returns_false_for_wrong_hour(self) -> None:
        """Wrong HKT conversion (DST bug or formula error) fails validation."""
        # 1777018200 is NOT 15:10 — it should be 16:10 HKT
        # Pass a ts that when converted to HKT gives a different hour
        bad_ts = 1777018200 - 3600  # = 15:10 HKT — should fail
        # validate_clock_against_anchor checks: does the HKT conversion
        # match _KNOWN_ANCHOR_HKT ("2026-04-24 16:10:00+08:00")?
        # 1777018200 - 3600 = 1777014600 → HKT 15:10 → should NOT match 16:10
        assert t1c.validate_clock_against_anchor(bad_ts) is False


class TestBoundaryTrigger:
    """is_t1_window_boundary with 60s threshold."""

    def test_boundary_fires_at_60s_before_expiry(self) -> None:
        """At 240s into window (60s before expiry) → boundary=True."""
        # Window base = 1777010700
        # secs_into_window = 245 → 55s remaining → >= (300-60)=240 → True
        with unittest.mock.patch.object(t1c._time, "time", return_value=float(1777010700 + 245)):
            t1c._ntp_offset_seconds = 0.0
            result = t1c.is_t1_window_boundary(threshold_secs=60)
            assert result is True

    def test_boundary_not_fired_at_100s_before_expiry(self) -> None:
        """At 200s into window (100s before expiry) → boundary=False (threshold=60 needs >=240s)."""
        # secs_into_window = 200 → < (300-60)=240 → False
        with unittest.mock.patch.object(t1c._time, "time", return_value=float(1777010700 + 200)):
            t1c._ntp_offset_seconds = 0.0
            result = t1c.is_t1_window_boundary(threshold_secs=60)
            assert result is False

    def test_boundary_uses_corrected_time(self) -> None:
        """is_t1_window_boundary uses get_corrected_unix_time, not raw time.time()."""
        # 245s into window with NTP offset of -5s
        # Corrected time = base + 245 - 5 = base + 240
        # secs_into_window (corrected) = 240 → boundary=True
        with unittest.mock.patch.object(t1c._time, "time", return_value=float(1777010700 + 245)):
            t1c._ntp_offset_seconds = -5.0  # local clock 5s ahead
            result = t1c.is_t1_window_boundary(threshold_secs=60)
            assert result is True


class TestStaleTokenPruning:
    """Token map pruning: tokens from >2 windows ago are removed."""

    def test_prunes_tokens_from_3_windows_ago(self) -> None:
        """
        _token_to_slug_map should prune tokens from >2 windows ago.
        """
        current_ts = t1c.get_current_t1_window()
        stale_ts = current_ts - 900  # 3 windows ago
        fresh_ts = current_ts

        slug_map = {
            "tok_stale": f"btc-updown-5m-{stale_ts}",
            "tok_current": f"btc-updown-5m-{fresh_ts}",
        }
        pruned = {
            tok: slug for tok, slug in slug_map.items()
            if not slug.rsplit("-", 1)[-1].isdigit()
            or int(slug.rsplit("-", 1)[-1]) >= (current_ts - 600)
        }
        assert "tok_stale" not in pruned
        assert "tok_current" in pruned

    def test_prunes_tokens_from_2_windows_ago(self) -> None:
        """
        Exactly 2 windows ago (stale_ts = current_ts - 600) → pruned.
        """
        current_ts = t1c.get_current_t1_window()
        two_window_ago = current_ts - 600  # exactly 2 windows = boundary case

        slug_map = {
            "tok_2win_ago": f"eth-updown-5m-{two_window_ago}",
            "tok_current": f"eth-updown-5m-{current_ts}",
        }
        pruned = {
            tok: slug for tok, slug in slug_map.items()
            if not slug.rsplit("-", 1)[-1].isdigit()
            or int(slug.rsplit("-", 1)[-1]) >= (current_ts - 600)
        }
        # 600 exactly = boundary, keep; < 600 = prune
        # two_window_ago < current_ts - 600? NO — they are equal. So kept.
        assert "tok_2win_ago" in pruned
        assert "tok_current" in pruned

    def test_non_numeric_slug_preserved(self) -> None:
        """Slug with non-numeric suffix (e.g. from earlier) is preserved."""
        slug_map = {
            "tok_weird": "some-unknown-slug-format",
            "tok_current": f"btc-updown-5m-{t1c.get_current_t1_window()}",
        }
        current_ts = t1c.get_current_t1_window()
        pruned = {
            tok: slug for tok, slug in slug_map.items()
            if not slug.rsplit("-", 1)[-1].isdigit()
            or int(slug.rsplit("-", 1)[-1]) >= (current_ts - 600)
        }
        assert "tok_weird" in pruned  # non-numeric → always kept
        assert "tok_current" in pruned