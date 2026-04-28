from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, replace

from panopticon_py.rate_limit_governor import RateLimitGovernor


@dataclass(frozen=True)
class FrictionSnapshot:
    network_ping_ms: float
    current_base_fee: float
    kyle_lambda: float
    gas_cost_estimate: float
    api_health: str
    l2_timeout_ms: float
    degraded: bool
    kelly_cap: float
    last_update_ts: float


class GlobalFrictionState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        now = time.time()
        self._snapshot = FrictionSnapshot(
            network_ping_ms=120.0,
            current_base_fee=0.0015,
            kyle_lambda=0.000012,
            gas_cost_estimate=0.25,
            api_health="ok",
            l2_timeout_ms=0.0,
            degraded=False,
            kelly_cap=0.25,
            last_update_ts=now,
        )

    def get(self) -> FrictionSnapshot:
        return self._snapshot

    def set(self, snapshot: FrictionSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot


class FrictionStateWorker:
    """Asynchronous worker updating friction snapshot every 100ms."""

    def __init__(self, state: GlobalFrictionState, interval_sec: float = 0.1) -> None:
        self.state = state
        self.interval_sec = interval_sec
        self.governor = RateLimitGovernor()
        self._thread: threading.Thread | None = None
        self._running = False

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
        while self._running:
            snap = self.state.get()

            ping = max(50.0, min(350.0, snap.network_ping_ms + random.uniform(-8, 8)))
            fee = max(0.0004, min(0.005, snap.current_base_fee + random.uniform(-0.0001, 0.0001)))
            kyle = max(0.000001, min(0.002, snap.kyle_lambda + random.uniform(-0.000001, 0.000001)))
            gas = max(0.02, min(4.0, snap.gas_cost_estimate + random.uniform(-0.05, 0.05)))

            l2_timeout = random.choice([120.0, 180.0, 250.0, 480.0, 530.0])
            degraded = l2_timeout > 500.0
            kelly_cap = 0.1 if degraded else 0.25

            # Combine governor signals into api health
            healthy = self.governor.allow("polymarket_book") and self.governor.allow("moralis_cu", 5)
            api_health = "ok" if healthy else "throttled"

            new_snap = replace(
                snap,
                network_ping_ms=ping,
                current_base_fee=fee,
                kyle_lambda=kyle,
                gas_cost_estimate=gas,
                api_health=api_health,
                l2_timeout_ms=l2_timeout,
                degraded=degraded,
                kelly_cap=kelly_cap,
                last_update_ts=time.time(),
            )
            self.state.set(new_snap)
            time.sleep(self.interval_sec)

