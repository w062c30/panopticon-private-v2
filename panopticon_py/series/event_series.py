"""
panopticon_py/series/event_series.py

D21 — Event Series Data Classes and Oracle Risk Classification.

Series types:
  DEADLINE_LADDER  — same topic, multiple settlement deadlines
  ROLLING_WINDOW   — same asset, consecutive time windows (T1 markets)
  CORRELATED_TOPIC — manually seeded topic clusters
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


SERIES_TYPE_DEADLINE_LADDER = "DEADLINE_LADDER"
SERIES_TYPE_ROLLING_WINDOW = "ROLLING_WINDOW"
SERIES_TYPE_CORRELATED_TOPIC = "CORRELATED_TOPIC"

ORACLE_RISK_LOW = "LOW"
ORACLE_RISK_MEDIUM = "MEDIUM"
ORACLE_RISK_HIGH = "HIGH"
ORACLE_RISK_UNKNOWN = "UNKNOWN"

# Keywords that imply LOW oracle risk (objective/binary events):
ORACLE_LOW_RISK_KEYWORDS = [
    "meeting-held", "deal-signed", "ceasefire-extended",
    "treaty", "election-held", "voted", "announced",
    "updown-5m", "up-or-down-5m",  # T1: price is objective
]

# Keywords that imply HIGH oracle risk (subjective/ambiguous events):
ORACLE_HIGH_RISK_KEYWORDS = [
    "invade", "attack", "normal", "significant", "major",
    "substantial", "large-scale", "effectively",
    "hormuz-traffic", "iran-permanent",
    "us-iran", "military-action",
]


def classify_oracle_risk(slug: str) -> str:
    """Classify oracle risk for a market based on slug keywords."""
    slug_lower = slug.lower()
    for kw in ORACLE_LOW_RISK_KEYWORDS:
        if kw in slug_lower:
            return ORACLE_RISK_LOW
    for kw in ORACLE_HIGH_RISK_KEYWORDS:
        if kw in slug_lower:
            return ORACLE_RISK_HIGH
    return ORACLE_RISK_MEDIUM


@dataclass
class SeriesMember:
    token_id: str
    slug: str
    settlement_date: Optional[datetime] = None
    market_tier: str = "t2"
    current_prob: float = 0.5


@dataclass
class EventSeries:
    series_id: str
    series_type: str
    members: list[SeriesMember] = field(default_factory=list)
    underlying_topic: str = ""
    oracle_risk: str = ORACLE_RISK_UNKNOWN

    def members_by_deadline(self) -> list[SeriesMember]:
        """Return members sorted earliest→latest settlement (for DEADLINE_LADDER)."""
        return sorted(
            [m for m in self.members if m.settlement_date],
            key=lambda m: m.settlement_date,
        )


@dataclass
class MonotoneViolation:
    series_id: str
    earlier_slug: str
    later_slug: str
    gap_pct: float  # earlier.prob - later.prob (positive = violation)
    direction: str  # "EARLY_OVERPRICED" | "LATE_UNDERPRICED"