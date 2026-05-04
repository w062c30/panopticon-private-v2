# Panopticon config package

import os

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
def get_z_threshold() -> float:
    return SHADOW_MIN_ENTROPY_Z_THRESHOLD if SHADOW_MODE else MIN_ENTROPY_Z_THRESHOLD


def get_min_history_for_z() -> int:
    """
    D159: H-samples required before zscore_of_latest_delta() is meaningful.
    Default 5 (was 12 prod / 6 shadow) — low-frequency T2 needs fewer samples.
    """
    raw = os.getenv("HUNT_MIN_HISTORY_FOR_Z", "5").strip()
    v = int(raw)
    if v < 3:
        raise ValueError(f"HUNT_MIN_HISTORY_FOR_Z must be >= 3, got {v}")
    return v
