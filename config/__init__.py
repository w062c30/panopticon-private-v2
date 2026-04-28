# Panopticon config package

import os

# ── Shadow Mode ────────────────────────────────────────────────────────────
# Set PANOPTICON_SHADOW=1 in environment to enable shadow mode calibration.
# In shadow mode the system runs with relaxed thresholds but does NOT execute trades.
SHADOW_MODE = os.getenv("PANOPTICON_SHADOW", "0") == "1"

# ── EntropyWindow thresholds ───────────────────────────────────────────────
# SHADOW values (architect ruling 2026-04-24): used when SHADOW_MODE=True
SHADOW_MIN_ENTROPY_Z_THRESHOLD = -2.0
SHADOW_MIN_HISTORY_FOR_Z = 6

# PRODUCTION values: used when SHADOW_MODE=False
MIN_ENTROPY_Z_THRESHOLD = -4.0
MIN_HISTORY_FOR_Z = 12

# Convenience exports
def get_z_threshold() -> float:
    return SHADOW_MIN_ENTROPY_Z_THRESHOLD if SHADOW_MODE else MIN_ENTROPY_Z_THRESHOLD

def get_min_history_for_z() -> int:
    return SHADOW_MIN_HISTORY_FOR_Z if SHADOW_MODE else MIN_HISTORY_FOR_Z
