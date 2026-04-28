"""Rolling Shannon entropy with stale-buffer flush and trigger lock (WS gap / reconnect)."""

from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque


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

    window_sec: float = 5.0   # 5s window: 12 samples ≈ fills in 5-15s at 1-3 trades/s (T1/LIVE markets)
    gap_flush_sec: float = float("inf")  # disable auto-flush; only mark_reconnect() flushes
    max_internal_gap_sec: float = float("inf")
    min_history_for_z: int = 12

    _events: Deque[tuple[float, float, float]] = field(default_factory=deque)
    _last_recv_mono: float | None = None
    _h_history: Deque[float] = field(default_factory=deque)
    _trigger_locked: bool = False
    _healthy_span: float = 0.0
    _last_reason: str = ""

    def __post_init__(self) -> None:
        self.window_sec = float(os.getenv("HUNT_ENTROPY_WINDOW_SEC", str(self.window_sec)))
        self.gap_flush_sec = float(os.getenv("HUNT_ENTROPY_GAP_FLUSH_SEC", str(self.gap_flush_sec)))
        self.max_internal_gap_sec = float(os.getenv("HUNT_ENTROPY_MAX_INTERNAL_GAP_SEC", str(self.max_internal_gap_sec)))
        # Shadow mode: allow env override, else fall back to class default (12)
        shadow_override = os.getenv("HUNT_MIN_HISTORY_FOR_Z")
        if shadow_override is not None:
            self.min_history_for_z = int(shadow_override)

    def mark_reconnect(self) -> None:
        self._flush("ws_reconnect")

    def _flush(self, reason: str) -> None:
        self._events.clear()
        self._trigger_locked = True
        self._healthy_span = 0.0
        self._last_recv_mono = None
        self._last_reason = reason

    def push(self, recv_mono: float, buy_vol: float, sell_vol: float) -> str | None:
        """
        Push one tick. ``recv_mono`` should be ``time.monotonic()`` at receive time.
        Returns reason string if buffer was flushed, else None.
        """
        flushed: str | None = None
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

        if self._trigger_locked and len(self._events) >= 30:
            self._trigger_locked = False
        if self._trigger_locked and self._healthy_span >= 5.0:
            self._trigger_locked = False
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
