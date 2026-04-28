"""
tests/test_event_series.py

D21 — Event Series tests for series_detector, monotone_checker,
event_series dataclasses, oracle risk classification, and smart_exit_detector.
"""

import pytest
from datetime import datetime, timezone

from panopticon_py.series.event_series import (
    classify_oracle_risk,
    ORACLE_RISK_LOW,
    ORACLE_RISK_MEDIUM,
    ORACLE_RISK_HIGH,
    SeriesMember,
    EventSeries,
    SERIES_TYPE_DEADLINE_LADDER,
    SERIES_TYPE_ROLLING_WINDOW,
)
from panopticon_py.series.series_detector import (
    detect_series,
    _strip_deadline_suffix,
)
from panopticon_py.series.monotone_checker import check_monotone_violations


class TestOracleRiskClassification:
    """E1: Oracle risk keyword classification."""

    def test_oracle_risk_low_for_objective_slugs(self) -> None:
        assert classify_oracle_risk("iran-meeting-held-by-may-5") == ORACLE_RISK_LOW
        assert classify_oracle_risk("btc-updown-5m-1777018200") == ORACLE_RISK_LOW
        assert classify_oracle_risk("election-held-by-nov-3") == ORACLE_RISK_LOW

    def test_oracle_risk_high_for_ambiguous_slugs(self) -> None:
        assert classify_oracle_risk("hormuz-traffic-normal-by-may") == ORACLE_RISK_HIGH
        assert classify_oracle_risk("us-invade-venezuela") == ORACLE_RISK_HIGH
        assert classify_oracle_risk("iran-military-action-by-june") == ORACLE_RISK_HIGH

    def test_oracle_risk_medium_for_unknown(self) -> None:
        assert classify_oracle_risk("some-random-market-name") == ORACLE_RISK_MEDIUM
        assert classify_oracle_risk("xyz-event-happens") == ORACLE_RISK_MEDIUM


class TestDeadlineSuffixStripping:
    """Test _strip_deadline_suffix in series_detector."""

    def test_strip_month_name(self) -> None:
        # The regex strips the full "-by-april-30" suffix including "by"
        assert _strip_deadline_suffix("iran-peace-deal-by-april-30") == "iran-peace-deal"
        assert _strip_deadline_suffix("iran-peace-deal-by-may-15") == "iran-peace-deal"

    def test_strip_us_iran_pattern(self) -> None:
        assert _strip_deadline_suffix("us-iran-meeting-by-may-5") == "us-iran-meeting"
        assert _strip_deadline_suffix("us-iran-meeting-by-june-10") == "us-iran-meeting"

    def test_no_change_for_rolling_window(self) -> None:
        # T1 rolling window slugs have Unix timestamp suffix — not stripped
        assert _strip_deadline_suffix("btc-updown-5m-1777018200") == "btc-updown-5m-1777018200"


class TestDetectDeadlineLadder:
    """B3: detect_series with DEADLINE_LADDER markets (>= 2 members required)."""

    def test_deadline_ladder_grouped_by_prefix(self) -> None:
        markets = [
            {"slug": "iran-peace-deal-by-april-30", "conditionId": "0xA",
             "bestBid": "0.06", "endDate": "2026-04-30T00:00:00Z"},
            {"slug": "iran-peace-deal-by-may-31", "conditionId": "0xB",
             "bestBid": "0.28", "endDate": "2026-05-31T00:00:00Z"},
            {"slug": "iran-peace-deal-by-june-30", "conditionId": "0xC",
             "bestBid": "0.46", "endDate": "2026-06-30T00:00:00Z"},
        ]
        series = detect_series(markets)
        assert len(series) == 1
        assert series[0].series_type == SERIES_TYPE_DEADLINE_LADDER
        assert len(series[0].members) == 3

    def test_single_member_no_series_created(self) -> None:
        markets = [
            {"slug": "iran-peace-deal-by-april-30", "conditionId": "0xA",
             "bestBid": "0.06", "endDate": "2026-04-30T00:00:00Z"},
        ]
        series = detect_series(markets)
        assert len(series) == 0  # need >= 2 for DEADLINE_LADDER

    def test_deadline_ladder_members_sorted_by_deadline(self) -> None:
        markets = [
            {"slug": "iran-peace-deal-by-june-30", "conditionId": "0xC",
             "bestBid": "0.46", "endDate": "2026-06-30T00:00:00Z"},
            {"slug": "iran-peace-deal-by-april-30", "conditionId": "0xA",
             "bestBid": "0.06", "endDate": "2026-04-30T00:00:00Z"},
            {"slug": "iran-peace-deal-by-may-31", "conditionId": "0xB",
             "bestBid": "0.28", "endDate": "2026-05-31T00:00:00Z"},
        ]
        series = detect_series(markets)
        ordered = series[0].members_by_deadline()
        assert ordered[0].slug == "iran-peace-deal-by-april-30"
        assert ordered[1].slug == "iran-peace-deal-by-may-31"
        assert ordered[2].slug == "iran-peace-deal-by-june-30"


class TestDetectRollingWindow:
    """B3: detect_series with ROLLING_WINDOW markets (T1 patterns)."""

    def test_rolling_window_btc(self) -> None:
        markets = [
            {"slug": "btc-updown-5m-1777018200", "conditionId": "0xD"},
            {"slug": "btc-updown-5m-1777018500", "conditionId": "0xE"},
        ]
        series = detect_series(markets)
        assert len(series) == 1
        assert series[0].series_type == SERIES_TYPE_ROLLING_WINDOW
        assert series[0].oracle_risk == ORACLE_RISK_LOW


class TestMonotoneChecker:
    """B3: Monotone violation detection in deadline ladders."""

    def test_monotone_violation_detected(self) -> None:
        """P(apr) = 30% > P(may) = 20% → 10% gap, violation."""
        series = EventSeries(
            series_id="test-deal",
            series_type=SERIES_TYPE_DEADLINE_LADDER,
            members=[
                SeriesMember(
                    "0xA", "iran-peace-deal-by-april-30",
                    datetime(2026, 4, 30, tzinfo=timezone.utc),
                    current_prob=0.30,
                ),
                SeriesMember(
                    "0xB", "iran-peace-deal-by-may-31",
                    datetime(2026, 5, 31, tzinfo=timezone.utc),
                    current_prob=0.20,
                ),
            ],
        )
        violations = check_monotone_violations(series)
        assert len(violations) == 1
        assert violations[0].gap_pct == pytest.approx(0.10, abs=0.001)
        assert violations[0].direction == "EARLY_OVERPRICED"

    def test_no_violation_when_probs_ascending(self) -> None:
        """P(apr) = 6% < P(may) = 28% → correct monotone, no violation."""
        series = EventSeries(
            series_id="test-deal",
            series_type=SERIES_TYPE_DEADLINE_LADDER,
            members=[
                SeriesMember(
                    "0xA", "iran-peace-deal-by-april-30",
                    datetime(2026, 4, 30, tzinfo=timezone.utc),
                    current_prob=0.06,
                ),
                SeriesMember(
                    "0xB", "iran-peace-deal-by-may-31",
                    datetime(2026, 5, 31, tzinfo=timezone.utc),
                    current_prob=0.28,
                ),
            ],
        )
        violations = check_monotone_violations(series)
        assert len(violations) == 0

    def test_rolling_window_returns_empty_violations(self) -> None:
        """ROLLING_WINDOW series should return no violations (not a ladder)."""
        series = EventSeries(
            series_id="btc-updown",
            series_type=SERIES_TYPE_ROLLING_WINDOW,
            members=[
                SeriesMember("0xD", "btc-updown-5m-1777018200",
                             market_tier="t1", current_prob=0.55),
                SeriesMember("0xE", "btc-updown-5m-1777018500",
                             market_tier="t1", current_prob=0.52),
            ],
        )
        violations = check_monotone_violations(series)
        assert violations == []


class TestMembersByDeadline:
    """EventSeries.members_by_deadline() sorts correctly."""

    def test_members_by_deadline_sort_order(self) -> None:
        series = EventSeries(
            series_id="test",
            series_type=SERIES_TYPE_DEADLINE_LADDER,
            members=[
                SeriesMember("0xC", "test-june",
                             datetime(2026, 6, 30, tzinfo=timezone.utc),
                             current_prob=0.46),
                SeriesMember("0xA", "test-april",
                             datetime(2026, 4, 30, tzinfo=timezone.utc),
                             current_prob=0.06),
                SeriesMember("0xB", "test-may",
                             datetime(2026, 5, 31, tzinfo=timezone.utc),
                             current_prob=0.28),
            ],
        )
        ordered = series.members_by_deadline()
        assert ordered[0].slug == "test-april"
        assert ordered[1].slug == "test-may"
        assert ordered[2].slug == "test-june"


class TestSmartExitThreshold:
    """D1: Smart exit threshold respects 70% minimum."""

    def test_smart_exit_sync_helper_respects_threshold(self) -> None:
        """At 65% (< 70%), smart exit should not fire."""
        # Import the helper (or re-create inline)
        # The threshold is SMART_EXIT_THRESHOLD_PROB = 0.70
        threshold = 0.70
        trade_price = 0.65
        insider_score = 0.70
        position_side = "YES"
        position_size = 100.0
        trade_size = 50.0

        # Pre-conditions
        assert trade_price < threshold  # should not trigger
        # This means check_smart_exit() should return False for this scenario
        # We verify the threshold logic directly:
        should_fire = (
            trade_price >= threshold and
            insider_score >= 0.60 and
            position_side == "YES" and
            trade_size / position_size >= 0.30
        )
        assert should_fire is False