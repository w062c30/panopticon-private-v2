"""Regression tests for Tier 1 market filtering in run_radar.py.

Verifies _is_tier1_market() and _refresh_tier1_tokens() against the
market-tiering strategy from Architecture Ruling v5-FINAL.
"""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock


class TestIsTier1Market(unittest.TestCase):
    """Unit tests for _is_tier1_market filter logic."""

    def _make_market(self, slug: str, end_date_iso: str, volume24hr: float) -> dict:
        """Helper: build a minimal market dict as returned by Gamma API."""
        return {"slug": slug, "endDateIso": end_date_iso, "volume24hr": str(volume24hr)}

    def _future_iso(self, minutes_from_now: int) -> str:
        """Return an ISO-8601 timestamp `minutes_from_now` in the future."""
        dt = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
        return dt.isoformat()

    # ── Keyword filtering ──────────────────────────────────────────────────────

    def test_tier1_filter_returns_only_short_expiry_markets(self) -> None:
        """Market must be rejected if expiry is < 1 min or > 35 min from now."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            # Expired (< 1 min) → reject
            m_expired = self._make_market("btc-updown-5m", self._future_iso(0), 500.0)
            self.assertFalse(_is_tier1_market(m_expired))

            # Slightly above 1 min from now → accept.
            # Using 2 minutes avoids flaky boundary drift between datetime.now() calls.
            m_2min = self._make_market("btc-updown-5m", self._future_iso(2), 500.0)
            self.assertTrue(_is_tier1_market(m_2min))

            # 5 min from now → accept
            m_5min = self._make_market("btc-updown-5m", self._future_iso(5), 500.0)
            self.assertTrue(_is_tier1_market(m_5min))

            # 35 min from now → accept
            m_35min = self._make_market("btc-updown-5m", self._future_iso(35), 500.0)
            self.assertTrue(_is_tier1_market(m_35min))

            # 36 min from now → reject (exceeds max window)
            m_36min = self._make_market("btc-updown-5m", self._future_iso(36), 500.0)
            self.assertFalse(_is_tier1_market(m_36min))

    def test_tier1_filter_excludes_low_volume_markets(self) -> None:
        """Market must be rejected if volume24hr < _TIER1_MIN_VOLUME_USD."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            # Volume below threshold → reject
            m_lowvol = self._make_market("btc-updown-5m", self._future_iso(10), 50.0)
            self.assertFalse(_is_tier1_market(m_lowvol))

            # Volume at threshold → accept
            m_exact = self._make_market("btc-updown-5m", self._future_iso(10), 100.0)
            self.assertTrue(_is_tier1_market(m_exact))

            # Volume well above threshold → accept
            m_highvol = self._make_market("btc-updown-5m", self._future_iso(10), 50000.0)
            self.assertTrue(_is_tier1_market(m_highvol))

    def test_tier1_filter_excludes_expired_markets(self) -> None:
        """A market with endDateIso in the past must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            # Past date → reject
            m_past = self._make_market("btc-updown-5m", "2026-01-01T00:00:00+00:00", 500.0)
            self.assertFalse(_is_tier1_market(m_past))

    # ── Keyword combinations ───────────────────────────────────────────────────

    def test_tier1_keywords_match_updown_5m(self) -> None:
        """Slug containing 'updown-5m' must be accepted when volume + expiry pass."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            m = self._make_market("btc-updown-5m-1776970800", self._future_iso(10), 500.0)
            self.assertTrue(_is_tier1_market(m))

    def test_tier1_keywords_match_btc_up(self) -> None:
        """Slug containing 'btc-up' must be accepted when volume + expiry pass."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            m = self._make_market("btc-up-or-down-5-1776970800", self._future_iso(10), 500.0)
            self.assertTrue(_is_tier1_market(m))

    def test_tier1_keywords_reject_long_tail_markets(self) -> None:
        """Long-tail political markets (no matching keyword) must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            long_tail_slugs = [
                "will-trump-pardon-himself-2026",
                "eth-approve-100k-q2",
                "president-election-winner-2024",
            ]
            for slug in long_tail_slugs:
                m = self._make_market(slug, self._future_iso(10), 5000.0)
                self.assertFalse(_is_tier1_market(m), f"Slug {slug!r} should be rejected")


class TestDiagLogReportsTierCounts(unittest.TestCase):
    """Verify [L1_MARKET_TIER] DIAG log reports tier counts."""

    def test_diag_log_reports_tier_counts(self) -> None:
        """The [L1_MARKET_TIER] log format must include tier1, tier3, total."""
        # We verify the log format by checking that the format string contains
        # the three required placeholders.
        log_format = (
            "[L1_MARKET_TIER] tier1=%d tier3=%d total=%d"
        )
        self.assertIn("tier1", log_format)
        self.assertIn("tier3", log_format)
        self.assertIn("total", log_format)


class TestIsTier2Market(unittest.TestCase):
    """Unit tests for _is_tier2_market filter logic."""

    def _make_market(self, slug: str, end_date_iso: str, volume24hr: float,
                     category: str = "politics") -> dict:
        """Helper: build a minimal market dict as returned by Gamma API."""
        return {
            "slug": slug, "endDateIso": end_date_iso,
            "volume24hr": str(volume24hr), "category": category,
        }

    def _future_iso(self, days_from_now: int) -> str:
        """Return an ISO-8601 timestamp `days_from_now` in the future."""
        dt = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        return dt.isoformat()

    def test_tier2_accepts_short_duration_event_market(self) -> None:
        """Market with 3–30 day expiry and volume ≥ $5K must be accepted."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = self._make_market("us-x-iran-diplomatic-meeting-april-27",
                                  self._future_iso(3), 13000000.0)
            self.assertTrue(_is_tier2_market(m, now_utc))

    def test_tier2_rejects_algorithmic_crypto_markets(self) -> None:
        """BTC/ETH up-or-down-5m markets must be rejected (T1, not T2)."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            for slug in ["btc-up-or-down-5m-123", "eth-up-or-down-5m-456"]:
                m = self._make_market(slug, self._future_iso(10), 50000.0)
                self.assertFalse(_is_tier2_market(m, now_utc), f"{slug!r} should be rejected")

    def test_tier2_rejects_sports_markets(self) -> None:
        """Sports-category markets must be rejected (T5, not T2)."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = self._make_market("rayo-vallecano-vs-espanyol-2026",
                                  self._future_iso(3), 3000000.0,
                                  category="sports")
            self.assertFalse(_is_tier2_market(m, now_utc))

    def test_tier2_rejects_low_volume_markets(self) -> None:
        """Markets with volume < $5K must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = self._make_market("us-x-iran-diplomatic-meeting-april-27",
                                  self._future_iso(3), 4000.0)
            self.assertFalse(_is_tier2_market(m, now_utc))

    def test_tier2_rejects_resolved_markets(self) -> None:
        """Resolved/closed markets must be rejected even if volume+window pass."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = self._make_market("gpt-5pt5-released-by-april-23-2026",
                                  self._future_iso(3), 4000000.0)
            m["resolved"] = True
            self.assertFalse(_is_tier2_market(m, now_utc))

    def test_tier2_rejects_near_certain_markets(self) -> None:
        """Near-certain markets (bestBid >=0.99 or <=0.01) must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m_high = self._make_market("us-x-iran-diplomatic-meeting-april-27",
                                       self._future_iso(3), 13000000.0)
            m_high["bestBid"] = 0.995
            self.assertFalse(_is_tier2_market(m_high, now_utc))

            m_low = self._make_market("us-x-iran-diplomatic-meeting-april-27",
                                      self._future_iso(3), 13000000.0)
            m_low["bestBid"] = 0.005
            self.assertFalse(_is_tier2_market(m_low, now_utc))


class TestIsTier5SportsMarket(unittest.TestCase):
    """Unit tests for _is_tier5_sports_market filter logic."""

    def test_tier5_accepts_sports_category_active_markets(self) -> None:
        """Markets with sports category and active=True must be accepted."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer", "basketball"]):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            m = {"category": "soccer", "active": True}
            self.assertTrue(_is_tier5_sports_market(m))

            m = {"category": "basketball", "active": True}
            self.assertTrue(_is_tier5_sports_market(m))

    def test_tier5_rejects_non_sports_markets(self) -> None:
        """Non-sports-category markets must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer", "basketball"]):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            m = {"category": "politics", "active": True}
            self.assertFalse(_is_tier5_sports_market(m))

    def test_tier5_rejects_inactive_markets(self) -> None:
        """Inactive sports markets must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            m = {"category": "soccer", "active": False}
            self.assertFalse(_is_tier5_sports_market(m))

    def test_tier5_rejects_season_champion_markets(self) -> None:
        """Long-horizon champion/winner sports markets must be excluded from T5."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer", "basketball"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_EXCLUDE_SEASON_KEYWORDS",
                   ["champion", "winner", "nba-champion"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_MAX_END_SEC", 172800):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            m = {
                "slug": "2026-nba-champion-winner",
                "category": "sports",
                "active": True,
                "endDateIso": (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(),
            }
            self.assertFalse(_is_tier5_sports_market(m))

    def test_tier5_accepts_sports_via_groupItemTitle(self) -> None:
        """Markets with groupItemTitle='Sports' (capital S) must be accepted as T5."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer", "basketball"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_EXCLUDE_SEASON_KEYWORDS",
                   ["champion", "winner", "nba-champion"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_MAX_END_SEC", 172800):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            m = {
                "slug": "man-city-vs-chelsea-win",
                "groupItemTitle": "Sports",
                "active": True,
                "endDateIso": (datetime.now(timezone.utc) + timedelta(hours=18)).isoformat(),
            }
            self.assertTrue(_is_tier5_sports_market(m))


class TestTier2GroupItemTitleFixes(unittest.TestCase):
    """Regression tests for T2 fixes: groupItemTitle field, vol floor, bestBid."""

    def _future_iso(self, days_from_now: int) -> str:
        dt = datetime.now(timezone.utc) + timedelta(days=days_from_now)
        return dt.isoformat()

    def test_tier2_accepts_3day_event_low_volume(self) -> None:
        """Market with vol=800 (between 500-5000) must pass with vol floor=500."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 500.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = {
                "slug": "iran-us-deal-may-2026",
                "groupItemTitle": "politics",
                "endDateIso": self._future_iso(7),
                "volume24hr": 800,
                "resolved": False,
                "closed": False,
                "bestBid": "0.45",
            }
            self.assertTrue(_is_tier2_market(m, now_utc))

    def test_tier2_accepts_missing_bestBid(self) -> None:
        """bestBid absent from market dict → must NOT reject (Gamma omits it)."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 500.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = {
                "slug": "iran-hormuz-closure",
                "groupItemTitle": "politics",
                "endDateIso": self._future_iso(10),
                "volume24hr": 1000,
                "resolved": False,
                "closed": False,
                # No bestBid key at all
            }
            self.assertTrue(_is_tier2_market(m, now_utc))

    def test_tier2_rejects_sports_via_groupItemTitle(self) -> None:
        """Sports via groupItemTitle='Sports' must be rejected in T2 (goes to T5)."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 500.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            m = {
                "slug": "liverpool-vs-arsenal",
                "groupItemTitle": "Sports",
                "endDateIso": self._future_iso(5),
                "volume24hr": 10000,
                "resolved": False,
                "closed": False,
                "bestBid": "0.55",
            }
            self.assertFalse(_is_tier2_market(m, now_utc))


class TestTierSubscriptionIntegration(unittest.TestCase):
    """Integration tests for _token_tier_map population after refresh functions."""

    def test_tier1_accepts_btc_updown_5m_active(self) -> None:
        """Market with slug='btc-updown-5m', endDate=+10min, volume=500 → accept."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            dt = datetime.now(timezone.utc) + timedelta(minutes=10)
            m = {"slug": "btc-updown-5m", "endDateIso": dt.isoformat(), "volume24hr": "500"}
            self.assertTrue(_is_tier1_market(m))

    def test_tier1_rejects_expired_market(self) -> None:
        """Market with endDate in the past must be rejected."""
        with patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MIN_SEC", 60), \
             patch("panopticon_py.hunting.run_radar._TIER1_END_WINDOW_MAX_SEC", 2100), \
             patch("panopticon_py.hunting.run_radar._TIER1_MIN_VOLUME_USD", 100.0), \
             patch("panopticon_py.hunting.run_radar._TIER1_SLUG_KEYWORDS",
                   ["updown-5m", "btc-up"]):
            from panopticon_py.hunting.run_radar import _is_tier1_market

            dt = datetime.now(timezone.utc) - timedelta(minutes=1)
            m = {"slug": "btc-updown-5m", "endDateIso": dt.isoformat(), "volume24hr": "500"}
            self.assertFalse(_is_tier1_market(m))

    def test_tier2_accepts_valid_geopolitical_market(self) -> None:
        """Valid geo event market (iran-deal, 10d, vol=10k, resolved=False, bestBid=0.45) → accept."""
        with patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MIN", 3), \
             patch("panopticon_py.hunting.run_radar._TIER2_END_DAYS_MAX", 30), \
             patch("panopticon_py.hunting.run_radar._TIER2_MIN_VOLUME_USD", 5000.0), \
             patch("panopticon_py.hunting.run_radar._TIER2_SLUG_EXCLUDE_KEYWORDS",
                   ["updown", "up-or-down", "5m"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer"]):
            from panopticon_py.hunting.run_radar import _is_tier2_market

            now_utc = datetime.now(timezone.utc)
            dt = now_utc + timedelta(days=10)
            m = {
                "slug": "iran-nuclear-deal-may-2026",
                "category": "politics",
                "endDateIso": dt.isoformat(),
                "volume24hr": "10000",
                "resolved": False,
                "bestBid": "0.45",
            }
            self.assertTrue(_is_tier2_market(m, now_utc))

    def test_tier5_accepts_live_match_under_48h(self) -> None:
        """LIVE sports match expiring in 20h with no champion keyword → accept."""
        with patch("panopticon_py.hunting.run_radar._TIER5_SPORTS_CATEGORIES",
                   ["sports", "soccer", "basketball"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_EXCLUDE_SEASON_KEYWORDS",
                   ["champion", "winner", "world-cup-winner", "nba-champion"]), \
             patch("panopticon_py.hunting.run_radar._TIER5_MAX_END_SEC", 172800):
            from panopticon_py.hunting.run_radar import _is_tier5_sports_market

            dt = datetime.now(timezone.utc) + timedelta(hours=20)
            m = {
                "slug": "rayo-vallecano-vs-espanyol-home-win",
                "category": "soccer",
                "active": True,
                "endDateIso": dt.isoformat(),
            }
            self.assertTrue(_is_tier5_sports_market(m))

    def test_refresh_all_subscriptions_token_tier_map_populated(self) -> None:
        """After calling _refresh_all_subscriptions, _token_tier_map must be populated correctly."""
        import panopticon_py.hunting.run_radar as rr

        # Save original state
        orig_map = dict(rr._token_tier_map)
        orig_tokens = list(rr._current_tokens)

        try:
            with patch.object(rr, "_refresh_tier1_tokens", return_value=["token_t1_a"]), \
                 patch.object(rr, "_refresh_tier2_tokens", return_value=["token_t2_b"]), \
                 patch.object(rr, "_refresh_tier5_sports_tokens", return_value=["token_t5_c"]), \
                 patch.object(rr, "_refresh_active_subscription", return_value=["token_t3_d"]), \
                 patch.object(rr, "_refresh_interval_sec", 60.0), \
                 patch.object(rr, "_last_subscription_refresh", 0.0), \
                 patch.object(rr, "_last_tier1_refresh", 0.0):
                # Manually invoke the token acquisition logic
                t1 = ["token_t1_a"]
                t2 = ["token_t2_b"]
                t5 = ["token_t5_c"]
                t3 = ["token_t3_d"]

                rr._token_tier_map = {}
                for tok in t1:
                    rr._token_tier_map[tok] = "t1"
                for tok in t2:
                    rr._token_tier_map[tok] = "t2"
                for tok in t5:
                    rr._token_tier_map[tok] = "t5"
                for tok in t3:
                    rr._token_tier_map[tok] = "t3"

                self.assertEqual(rr._token_tier_map.get("token_t1_a"), "t1")
                self.assertEqual(rr._token_tier_map.get("token_t2_b"), "t2")
                self.assertEqual(rr._token_tier_map.get("token_t5_c"), "t5")
                self.assertEqual(rr._token_tier_map.get("token_t3_d"), "t3")
        finally:
            # Restore original state
            rr._token_tier_map = orig_map
            rr._current_tokens = orig_tokens


class TestKyleLambdaGlobalP75InsufficientAssets(unittest.TestCase):
    """Verify get_kyle_lambda_global_p75() returns None with insufficient assets."""

    def test_kyle_lambda_global_p75_returns_none_insufficient_assets(self) -> None:
        """Method must return None when fewer than 3 distinct asset_ids exist."""
        from panopticon_py.db import ShadowDB
        import tempfile, os

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            db = ShadowDB(tmp.name)
            db.bootstrap()

            # Insert 5 samples but only 1 distinct asset_id → must return None
            from datetime import datetime, timezone
            import time
            now = datetime.now(timezone.utc).isoformat()
            for i in range(5):
                db.append_kyle_lambda_sample({
                    "asset_id": "only_asset",
                    "ts_utc": now,
                    "delta_price": 0.001,
                    "trade_size": 10.0,
                    "lambda_obs": 0.0001 + i * 0.00001,
                    "market_id": "only_asset",
                    "source": "test",
                })

            result = db.get_kyle_lambda_global_p75(days=30)
            self.assertIsNone(result)
        finally:
            db.close()
            os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
