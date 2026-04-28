from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from panopticon_py.cognitive import build_cognitive_signal


@dataclass(frozen=True)
class CognitiveSnapshot:
    trust_score: float
    signal_reliability: float
    external_event_score: float
    sentiment_score: float
    kelly_cap: float
    degraded: bool
    timeout_ms: float
    graph_disabled: bool
    funding_risk: str
    behavior_score: float
    updated_ts: float


class CognitiveCache:
    def __init__(self) -> None:
        now = time.time()
        self._snapshot = CognitiveSnapshot(
            trust_score=0.5,
            signal_reliability=0.5,
            external_event_score=0.5,
            sentiment_score=0.5,
            kelly_cap=0.25,
            degraded=False,
            timeout_ms=0.0,
            graph_disabled=False,
            funding_risk="NORMAL",
            behavior_score=0.5,
            updated_ts=now,
        )
        self._lock = threading.Lock()

    def get(self) -> CognitiveSnapshot:
        return self._snapshot

    def set(self, snapshot: CognitiveSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


class CognitiveWorker:
    """Background pre-compute worker. Main path only reads snapshot."""

    def __init__(self, cache: CognitiveCache, interval_sec: float = 0.3) -> None:
        self.cache = cache
        self.interval_sec = interval_sec
        self._running = False
        self._thread: threading.Thread | None = None
        self._headlines = [
            "Whale wallet accumulated YES tokens within 5 minutes.",
            "Major media rumor triggered short-term sentiment spike.",
            "No external confirmation for the previous social signal.",
        ]
        self._funding_labels = [
            "direct_bridge",
            "binance_hot_wallet",
            "tornado_cash_relay",
        ]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _loop(self) -> None:
        idx = 0
        while self._running:
            start = time.time()
            text = self._headlines[idx % len(self._headlines)]
            funding = self._funding_labels[idx % len(self._funding_labels)]
            idx += 1

            signal = build_cognitive_signal(
                wallet_features={
                    "funding_depth": 0.8,
                    "split_order_score": 0.7,
                    "funding_source_label": funding,
                    "temporal_hour_utc": 3,
                    "temporal_minute_utc": 10,
                    "has_preflight_tx": bool(idx % 2),
                },
                headline=text,
                use_llm=False,
            )
            elapsed_ms = (time.time() - start) * 1000
            degraded = elapsed_ms > 500
            kelly_cap = 0.1 if degraded else 0.25
            self.cache.set(
                CognitiveSnapshot(
                    trust_score=signal.trust_score,
                    signal_reliability=signal.signal_reliability,
                    external_event_score=signal.external_event_score,
                    sentiment_score=signal.sentiment_score,
                    kelly_cap=kelly_cap,
                    degraded=degraded,
                    timeout_ms=elapsed_ms,
                    graph_disabled=signal.graph_disabled,
                    funding_risk=signal.funding_risk,
                    behavior_score=signal.behavior_score,
                    updated_ts=time.time(),
                )
            )
            time.sleep(self.interval_sec)

