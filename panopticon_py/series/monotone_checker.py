"""
panopticon_py/series/monotone_checker.py

D21 — Monotone Probability Violation Checker.

For DEADLINE_LADDER series: probability must be non-decreasing
as deadline approaches (later settlement >= earlier settlement).
Violation threshold: gap > 2% to filter noise.

In Phase 3, violations are logged only (action_taken=LOGGED).
No trade signal generated yet — requires architect review.
"""

from __future__ import annotations

import logging

from .event_series import EventSeries, MonotoneViolation

logger = logging.getLogger(__name__)

MONOTONE_VIOLATION_THRESHOLD = 0.02  # 2% gap minimum to report


def check_monotone_violations(series: EventSeries) -> list[MonotoneViolation]:
    """
    Check a DEADLINE_LADDER series for monotone probability violations.

    For any pair (earlier_deadline, later_deadline):
      P(earlier) must be <= P(later). If P(earlier) > P(later) + threshold,
      this is a pricing anomaly — log it.

    Args:
        series: EventSeries with DEADLINE_LADDER type and members_by_deadline()

    Returns:
        list of MonotoneViolation objects (may be empty)
    """
    if series.series_type != "DEADLINE_LADDER":
        return []

    violations: list[MonotoneViolation] = []
    members = series.members_by_deadline()

    for i in range(len(members) - 1):
        earlier = members[i]
        later = members[i + 1]

        gap = earlier.current_prob - later.current_prob
        if gap > MONOTONE_VIOLATION_THRESHOLD:
            direction = (
                "EARLY_OVERPRICED"
                if earlier.current_prob > later.current_prob
                else "LATE_UNDERPRICED"
            )
            v = MonotoneViolation(
                series_id=series.series_id,
                earlier_slug=earlier.slug,
                later_slug=later.slug,
                gap_pct=gap,
                direction=direction,
            )
            logger.info(
                "[SERIES][MONOTONE_VIOLATION] series=%s "
                "earlier=%s(%.0f%%) later=%s(%.0f%%) gap=%.1f%% direction=%s",
                series.series_id,
                earlier.slug[-30:], earlier.current_prob * 100,
                later.slug[-30:], later.current_prob * 100,
                gap * 100,
                direction,
            )
            violations.append(v)

    return violations