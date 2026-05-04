# Panopticon config package

import logging
import os

_logger = logging.getLogger(__name__)

# ── Shadow Mode ────────────────────────────────────────────────────────────
# Set PANOPTICON_SHADOW=1 in environment to enable shadow mode calibration.
# In shadow mode the system runs with relaxed thresholds but does NOT execute trades.
SHADOW_MODE = os.getenv("PANOPTICON_SHADOW", "0") == "1"

# ── EntropyWindow thresholds ───────────────────────────────────────────────
# SHADOW values (architect ruling 2026-04-24): used when SHADOW_MODE=True
SHADOW_MIN_ENTROPY_Z_THRESHOLD = -2.0

# PRODUCTION values: used when SHADOW_MODE=False
MIN_ENTROPY_Z_THRESHOLD = -4.0

# Convenience exports
_MIN_HISTORY_HIGH_WARNED = False


def get_z_threshold() -> float:
    """
    D164: Optional override HUNT_MIN_ENTROPY_Z_THRESHOLD (negative float).
    If unset, use shadow vs production constants from SHADOW_MODE.
    """
    raw = os.getenv("HUNT_MIN_ENTROPY_Z_THRESHOLD")
    if raw is not None and str(raw).strip() != "":
        v = float(str(raw).strip())
        if v > 0:
            raise ValueError(
                "HUNT_MIN_ENTROPY_Z_THRESHOLD must be negative (e.g. -3.5). "
                f"Got: {v}"
            )
        if v > -1.0:
            _logger.warning(
                "[EW] HUNT_MIN_ENTROPY_Z_THRESHOLD=%.2f is very loose — false positive rate will be high",
                v,
            )
        return v
    return SHADOW_MIN_ENTROPY_Z_THRESHOLD if SHADOW_MODE else MIN_ENTROPY_Z_THRESHOLD


def get_min_history_for_z() -> int:
    """
    D159: H-samples required before zscore_of_latest_delta() is meaningful.
    Default 5 (was 12 prod / 6 shadow) — low-frequency T2 needs fewer samples.
    D164: warn once if env requests a very large window (z_ready may stay rare).
    """
    global _MIN_HISTORY_HIGH_WARNED
    raw = os.getenv("HUNT_MIN_HISTORY_FOR_Z", "5").strip()
    v = int(raw)
    if v < 3:
        raise ValueError(
            f"HUNT_MIN_HISTORY_FOR_Z={v} is too low (min=3). "
            "Setting below 3 produces statistically meaningless z-scores."
        )
    if v > 50 and not _MIN_HISTORY_HIGH_WARNED:
        _logger.warning(
            "[EW] HUNT_MIN_HISTORY_FOR_Z=%d is very high — z_ready may rarely trigger on low-frequency markets",
            v,
        )
        _MIN_HISTORY_HIGH_WARNED = True
    return v
