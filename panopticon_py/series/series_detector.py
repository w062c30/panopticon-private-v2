"""
panopticon_py/series/series_detector.py

D21 — Event Series Detection from Gamma API market lists.

Three detection methods:
  1. DEADLINE_LADDER: slug prefix grouping with deadline suffix stripping
  2. ROLLING_WINDOW: trailing Unix timestamp patterns (T1 markets)
  3. CORRELATED_TOPIC: manual seed map (architect-maintained)

Usage:
  detected = detect_series(gamma_markets_raw)
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from .event_series import (
    EventSeries,
    SeriesMember,
    SERIES_TYPE_DEADLINE_LADDER,
    SERIES_TYPE_ROLLING_WINDOW,
    SERIES_TYPE_CORRELATED_TOPIC,
    classify_oracle_risk,
    ORACLE_RISK_LOW,
)

logger = logging.getLogger(__name__)

# Manual correlated topic map (architect-maintained seed)
CORRELATED_TOPIC_MAP: dict[str, list[str]] = {
    "iran-permanent-peace-deal": ["strait-of-hormuz-traffic"],
    "us-iran-diplomatic-meeting": [
        "strait-of-hormuz-traffic",
        "iran-permanent-peace-deal",
    ],
    "us-venezuela": [],
}

# Regex to strip trailing date/deadline from slug
# e.g. "iran-peace-deal-by-april-30" → "iran-peace-deal-by"
_DEADLINE_SUFFIX_RE = re.compile(
    r"-(by-)?(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"(uary|ruary|ch|il|e|y|ust|tember|ober|ember)?-?\d*"
    r"(-\d{4})?$",
    re.IGNORECASE,
)

# Match trailing Unix timestamp (9-11 digits) — T1 rolling window markets
_ROLLING_TS_RE = re.compile(r"-(\d{9,11})$")


def _strip_deadline_suffix(slug: str) -> str:
    """Remove trailing month-date pattern from slug to get base prefix."""
    return _DEADLINE_SUFFIX_RE.sub("", slug).rstrip("-").rstrip("-")


def detect_series(markets: list[dict]) -> list[EventSeries]:
    """
    Given a list of market dicts from Gamma API, detect and return EventSeries.

    Args:
        markets: list of Gamma API market dicts with keys like "slug",
                 "conditionId", "endDate", "bestBid", "market_tier"

    Returns:
        list of EventSeries (DEADLINE_LADDER and ROLLING_WINDOW types)
    """
    deadline_groups: dict[str, list[dict]] = defaultdict(list)
    rolling_groups: dict[str, list[dict]] = defaultdict(list)
    result: list[EventSeries] = []

    for m in markets:
        slug = str(m.get("slug") or "").lower()
        if not slug:
            continue

        # ── Check rolling window (T1 markets: btc-updown-5m-{ts}) ──────────────
        ts_match = _ROLLING_TS_RE.search(slug)
        if ts_match:
            prefix = slug[: ts_match.start()]
            rolling_groups[prefix].append(m)
            continue

        # ── Check deadline ladder (T2/T3 markets with date suffixes) ───────────
        prefix = _strip_deadline_suffix(slug)
        if prefix and prefix != slug:
            deadline_groups[prefix].append(m)

    # ── Build DEADLINE_LADDER series (>= 2 members) ─────────────────────────
    for prefix, group in deadline_groups.items():
        if len(group) < 2:
            continue
        members: list[SeriesMember] = []
        for m in group:
            slug = str(m.get("slug") or "").lower()
            token_id = str(m.get("conditionId") or m.get("token_id") or "")
            end_date_str = m.get("endDate") or m.get("end_date_iso") or ""
            end_dt: datetime | None = None
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(
                        end_date_str.replace("Z", "+00:00")
                    )
                except (ValueError, TypeError):
                    pass
            try:
                prob = float(m.get("bestBid") or m.get("best_bid") or 0.5)
            except (ValueError, TypeError):
                prob = 0.5
            members.append(SeriesMember(
                token_id=token_id,
                slug=slug,
                settlement_date=end_dt,
                market_tier=str(m.get("market_tier", "t2")),
                current_prob=prob,
            ))
        if not members:
            continue
        series_id = prefix.strip("-").strip()
        series = EventSeries(
            series_id=series_id,
            series_type=SERIES_TYPE_DEADLINE_LADDER,
            members=members,
            underlying_topic=series_id,
            oracle_risk=classify_oracle_risk(series_id),
        )
        result.append(series)

    # ── Build ROLLING_WINDOW series (>= 1 member) ────────────────────────────
    for prefix, group in rolling_groups.items():
        if not group:
            continue
        members: list[SeriesMember] = []
        try:
            sorted_group = sorted(
                group,
                key=lambda x: int(
                    _ROLLING_TS_RE.search(str(x.get("slug", ""))).group(1)
                    if _ROLLING_TS_RE.search(str(x.get("slug", "")))
                    else 0
                ),
            )
        except (ValueError, TypeError, AttributeError):
            sorted_group = group
        for m in sorted_group:
            slug = str(m.get("slug") or "").lower()
            token_id = str(m.get("conditionId") or m.get("token_id") or "")
            try:
                prob = float(m.get("bestBid") or m.get("best_bid") or 0.5)
            except (ValueError, TypeError):
                prob = 0.5
            members.append(SeriesMember(
                token_id=token_id,
                slug=slug,
                settlement_date=None,
                market_tier="t1",
                current_prob=prob,
            ))
        if not members:
            continue
        series_id = prefix.strip("-").strip()
        series = EventSeries(
            series_id=series_id or "t1-rolling",
            series_type=SERIES_TYPE_ROLLING_WINDOW,
            members=members,
            underlying_topic=prefix.strip("-").strip(),
            oracle_risk=ORACLE_RISK_LOW,  # T1 is always objective price
        )
        result.append(series)

    return result