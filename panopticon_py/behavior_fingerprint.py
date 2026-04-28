from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TemporalFeatures:
    hour_utc: int
    minute_utc: int
    has_preflight_tx: bool


def temporal_signature_score(features: TemporalFeatures) -> float:
    """Return a bounded behavioral score in [0,1] using simple temporal habits."""
    night_bias = 1.0 if 1 <= features.hour_utc <= 5 else 0.0
    minute_pattern = 1.0 if features.minute_utc % 5 == 0 else 0.3
    preflight = 1.0 if features.has_preflight_tx else 0.0
    raw = 0.45 * night_bias + 0.35 * minute_pattern + 0.2 * preflight
    return max(0.0, min(1.0, raw))
