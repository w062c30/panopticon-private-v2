from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    updated_at: float

    def try_take(self, amount: float = 1.0) -> bool:
        now = time.time()
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.updated_at = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimitGovernor:
    """Thread-safe in-memory rate limiter driven by API registry."""

    def __init__(self, registry_path: str = "config/api_capability_registry.json") -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}
        self._registry_path = Path(registry_path)
        self._load_defaults()

    def _load_defaults(self) -> None:
        if not self._registry_path.exists():
            return
        registry = json.loads(self._registry_path.read_text(encoding="utf-8"))
        now = time.time()
        polymarket = registry.get("apis", {}).get("polymarket", {}).get("rate_limits", {})
        if polymarket:
            # 1500 req / 10s => 150 req/s
            self._buckets["polymarket_book"] = TokenBucket(1500, 150.0, 1500, now)
            # 3500 req / 10s => 350 req/s
            self._buckets["polymarket_post_order"] = TokenBucket(3500, 350.0, 3500, now)
        binance = registry.get("apis", {}).get("binance", {}).get("rate_limits", {})
        if binance:
            # 6000 weight / minute => 100 weight/s
            self._buckets["binance_weight"] = TokenBucket(6000, 100.0, 6000, now)
        moralis = registry.get("apis", {}).get("moralis", {}).get("rate_limits", {})
        if moralis:
            # free/starter nominal 1000 CU/s
            self._buckets["moralis_cu"] = TokenBucket(1000, 1000.0, 1000, now)

    def allow(self, key: str, amount: float = 1.0) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                return True
            return bucket.try_take(amount)

