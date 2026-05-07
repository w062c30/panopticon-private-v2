"""Rolling Shannon entropy with stale-buffer flush and trigger lock (WS gap / reconnect).

D164-1 (z_ready / gate diagnostics, radar ``run_radar.py``):
``zscore_of_latest_delta`` returns (None, None) when ``_trigger_locked`` is True
(reconnect / gap flush), or when ``len(_h_history) < min_history_for_z``, or when
the delta tail has fewer than two points. Radar's ``history_not_ready`` counter
increments when z is None but the window is not locked (typically short H history).
``record_H_sample`` is only called after ``push`` + ``current_entropy()`` succeeds
(needs >=2 events in the rolling deque and not locked).

D165 (trigger lock unlock thresholds):
``_trigger_locked`` is cleared when EITHER condition is met (tested after each push):
  1. ``len(self._events) >= self._unlock_event_count``  (default 30; env HUNT_EW_UNLOCK_EVENT_COUNT)
  2. ``self._healthy_span >= self._unlock_healthy_span_sec``  (default 5.0s; env HUNT_EW_UNLOCK_HEALTHY_SPAN_SEC)
  3. D178: ``_last_recv_mono`` is set AND ``now - _last_recv_mono < _unlock_max_gap_sec`` AND ``len(_events) >= min(5, _unlock_event_count)``
     — safe unlock for tokens with active market data but no explicit reconnect events.
Low-frequency T2 markets that receive <1 tick/sec may never reach 30 events in a 5s window;
D165 adds env-driven overrides so that a 10-event / 3s-unlock is achievable.
D178 adds the gap-based safe-unlock to prevent permanent lockout when ``gap_flush_sec=float("inf")``.
"""

from __future__ import annotations

import logging as _logging
import math
import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque

_logger = _logging.getLogger(__name__)

_TIER_WINDOW_DEFAULTS: dict[str, float] = {
    "t1": 5.0,
    "t2": 60.0,
    "t3": 60.0,
    "t5": 60.0,
}


def _normalize_tier(tier: str) -> str:
    normalized = str(tier or "t3").strip().lower()
    return normalized if normalized in _TIER_WINDOW_DEFAULTS else "t3"


def resolve_window_sec_for_tier(tier: str, fallback: float | None = None) -> float:
    normalized_tier = _normalize_tier(tier)
    default_sec = _TIER_WINDOW_DEFAULTS.get(normalized_tier, 60.0) if fallback is None else float(fallback)
    return float(
        os.getenv(
            f"HUNT_ENTROPY_WINDOW_SEC_{normalized_tier.upper()}",
            os.getenv("HUNT_ENTROPY_WINDOW_SEC", str(default_sec)),
        )
    )


def _shannon_H(counts: dict[str, float]) -> float:
    tot = sum(max(0.0, v) for v in counts.values())
    if tot <= 0:
        return 0.0
    h = 0.0
    for v in counts.values():
        p = max(1e-18, v / tot)
        h -= p * math.log2(p)
    return h


@dataclass
class EntropyWindow:
    """
    Maintains ~``window_sec`` of (recv_mono, buy_vol, sell_vol) buckets.
    On recv gap > ``gap_flush_sec`` or ``mark_reconnect()`` → flush + lock triggers
    until ``window_sec`` of healthy consecutive samples (each gap <= max_internal_gap_sec).
    """

    tier: str = "t3"
    window_sec: float = 5.0   # tier-aware in D166; min H samples from HUNT_MIN_HISTORY_FOR_Z (default 5, D159)
    gap_flush_sec: float = float("inf")  # disable auto-flush; only mark_reconnect() flushes
    max_internal_gap_sec: float = float("inf")
    min_history_for_z: int = 5

    _events: Deque[tuple[float, float, float]] = field(default_factory=deque)
    _last_recv_mono: float | None = None
    _h_history: Deque[float] = field(default_factory=deque)
    _trigger_locked: bool = False
    _healthy_span: float = 0.0
    _last_reason: str = ""

    def __post_init__(self) -> None:
        self.tier = _normalize_tier(self.tier)
        self.window_sec = resolve_window_sec_for_tier(self.tier)
        self.gap_flush_sec = float(os.getenv("HUNT_ENTROPY_GAP_FLUSH_SEC", str(self.gap_flush_sec)))
        self.max_internal_gap_sec = float(os.getenv("HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC", str(self.max_internal_gap_sec)))
        # D159: single source — config.get_min_history_for_z() (HUNT_MIN_HISTORY_FOR_Z, default 5)
        from config import get_min_history_for_z

        self.min_history_for_z = get_min_history_for_z()
        # D165: unlock threshold controls (low-frequency T2 markets)
        self._unlock_event_count: int = int(
            os.getenv("HUNT_EW_UNLOCK_EVENT_COUNT", "30")
        )
        self._unlock_healthy_span_sec: float = float(
            os.getenv("HUNT_EW_UNLOCK_HEALTHY_SPAN_SEC", "5.0")
        )
        # D178: Safe unlock for tokens with no WS reconnect events but active market data.
        # Guards against permanently locked tokens when gap_flush_sec=float("inf").
        self._unlock_max_gap_sec: float = float(
            os.getenv("HUNT_EW_UNLOCK_MAX_GAP_SEC", "120.0")
        )
        if self._unlock_max_gap_sec < 30.0:
            raise ValueError(
                f"HUNT_EW_UNLOCK_MAX_GAP_SEC={self._unlock_max_gap_sec} is too low (min=30). "
                "Tokens need at least 30s of gap-free data before unlocking."
            )
        if self._unlock_event_count < 5:
            raise ValueError(
                f"HUNT_EW_UNLOCK_EVENT_COUNT={self._unlock_event_count} is too low (min=5). "
                "Fewer than 5 ticks produces statistically meaningless entropy."
            )
        if self._unlock_event_count > 200:
            _logger.warning(
                "[EW] HUNT_EW_UNLOCK_EVENT_COUNT=%d is very high — low-frequency markets may never unlock",
                self._unlock_event_count,
            )
        if self._unlock_healthy_span_sec < 1.0:
            raise ValueError(
                f"HUNT_EW_UNLOCK_HEALTHY_SPAN_SEC={self._unlock_healthy_span_sec} is too low (min=1.0s)."
            )
        if self.window_sec < 1.0 or self.window_sec > 600.0:
            raise ValueError(
                f"Entropy window_sec={self.window_sec} is invalid (must be in [1, 600])."
            )

    def refresh_subscription(self, reason: str = "sub_refresh") -> None:
        """
        D156-1: Called on subscription token list refresh (NOT actual WS disconnect).
        Does NOT flush _events or set _trigger_locked.
        Only logs the refresh for diagnostic purposes.
        Use mark_reconnect() ONLY for actual WS disconnects (ConnectionClosed/OSError).
        _h_history is preserved — H distribution is market-level state.
        """
        _logger.debug(
            "[EW][D156] refresh_subscription reason=%s h_hist=%d events=%d locked=%s",
            reason, len(self._h_history), len(self._events), self._trigger_locked,
        )

    # D154: reason parameter added for diagnostic differentiation
    # (subscription_refresh vs ws_disconnect, etc.)
    def mark_reconnect(self, reason: str = "ws_reconnect") -> None:
        """
        Called on WS reconnection (both real disconnect and subscription refresh).
        D154: _h_history intentionally NOT cleared — H distribution is market-level
        state that survives subscription refreshes. Only _events (tick buffer) is
        cleared because the tick sequence is discontinuous across reconnects.
        _trigger_locked is still set to ensure 30-event warm-up before next signal.
        """
        self._flush(reason)

    def _flush(self, reason: str) -> None:
        self._events.clear()
        self._trigger_locked = True
        self._healthy_span = 0.0
        self._last_recv_mono = None
        self._last_reason = reason
        _logger.debug(
            "[EW][D154] mark_reconnect reason=%s h_hist_preserved=%d",
            reason, len(self._h_history),
        )

    def push(self, recv_mono: float, buy_vol: float, sell_vol: float) -> str | None:
        """
        Push one tick. ``recv_mono`` should be ``time.monotonic()`` at receive time.
        Returns reason string if buffer was flushed, else None.
        """
        flushed: str | None = None
        prev_recv_mono = self._last_recv_mono
        if self._last_recv_mono is not None:
            dt = recv_mono - self._last_recv_mono
            if dt > self.gap_flush_sec:
                self._flush("recv_gap")
                flushed = "recv_gap"
            elif dt < 0:
                # Clock skew or synthetic tick — treat as zero-length gap, no penalty
                pass
            elif dt <= self.max_internal_gap_sec:
                self._healthy_span += min(dt, self.window_sec)
            else:
                self._healthy_span = max(0.0, self._healthy_span - dt * 0.1)

        self._last_recv_mono = recv_mono
        self._events.append((recv_mono, max(0.0, buy_vol), max(0.0, sell_vol)))
        cutoff = recv_mono - self.window_sec
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        if self._trigger_locked and len(self._events) >= self._unlock_event_count:
            self._trigger_locked = False
            _logger.debug("[EW][D165] unlocked via event_count=%d", len(self._events))
        if self._trigger_locked and self._healthy_span >= self._unlock_healthy_span_sec:
            self._trigger_locked = False
            _logger.debug("[EW][D165] unlocked via healthy_span=%.1f", self._healthy_span)
        # D178: Gap-based safe unlock — active tokens with no explicit reconnect events
        # can unlock after receiving a burst of data. Requires:
        #   (a) no gap detected (dt <= max_internal_gap_sec) on this push, AND
        #   (b) at least min(5, _unlock_event_count) events accumulated, AND
        #   (c) at least 2 samples in _h_history (entropy is meaningful)
        if self._trigger_locked and prev_recv_mono is not None:
            gap = recv_mono - prev_recv_mono
            min_events = min(5, self._unlock_event_count)
            if (gap <= self.max_internal_gap_sec
                    and len(self._events) >= min_events
                    and len(self._h_history) >= 2):
                self._trigger_locked = False
                _logger.info(
                    "[EW][D178] unlocked via gap_safe gap=%.1f events=%d h_hist=%d",
                    gap, len(self._events), len(self._h_history),
                )
        return flushed

    def current_entropy(self) -> float | None:
        if self._trigger_locked or len(self._events) < 2:
            return None
        buy = sum(e[1] for e in self._events)
        sell = sum(e[2] for e in self._events)
        if buy + sell <= 0:
            return None
        return _shannon_H({"buy": buy, "sell": sell})

    def record_H_sample(self, recv_mono: float) -> None:
        h = self.current_entropy()
        if h is None:
            return
        self._h_history.append(h)
        while len(self._h_history) > 200:
            self._h_history.popleft()

    def zscore_of_latest_delta(self) -> tuple[float | None, float | None]:
        """Return (delta_H, z) for last step; None if not enough data or locked."""
        if self._trigger_locked or len(self._h_history) < self.min_history_for_z:
            return None, None
        hs = list(self._h_history)
        if len(hs) < 2:
            return None, None
        d = hs[-1] - hs[-2]
        tail = hs[:-1]
        if len(tail) < 2:
            return d, None
        mu = sum(tail) / len(tail)
        var = sum((x - mu) ** 2 for x in tail) / max(1, len(tail) - 1)
        sigma = math.sqrt(var) if var > 1e-12 else 1e-6
        z = (d - 0.0) / sigma
        # Clamp z to [-50, 50] to prevent extreme outliers from crashing signal pipeline
        z = max(-50.0, min(50.0, z))
        return d, z

    def should_fire_negative_entropy(self, z_threshold: float = -4.0) -> bool:
        if self._trigger_locked:
            return False
        _d, z = self.zscore_of_latest_delta()
        if z is None:
            return False
        return z < z_threshold

    def state_dict(self) -> dict[str, Any]:
        return {
            "trigger_locked": self._trigger_locked,
            "healthy_span": self._healthy_span,
            "events": len(self._events),
            "h_hist": len(self._h_history),
        }
