"""Redis-backed seed whitelist (ZSET + meta). Requires ``REDIS_URL``."""

from __future__ import annotations

import json
import os
import time
from typing import Any


def seed_key_v1() -> str:
    return os.getenv("REDIS_SEED_KEY_V1", "panopticon:seed:v1")


def seed_meta_key() -> str:
    return os.getenv("REDIS_SEED_META_KEY", "panopticon:seed:meta")


def seed_key_v2() -> str:
    return os.getenv("REDIS_SEED_KEY_V2", "panopticon:seed:v2")


def seed_cluster_key(prefix: str) -> str:
    return f"panopticon:hunt:cluster:{prefix}"


def _require_redis():
    try:
        import redis  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError("Install redis: pip install redis") from e
    return redis


class RedisSeedStore:
    """ZSET ``panopticon:seed:v1`` score = heuristic rank score; member = lowercased address."""

    def __init__(self, url: str | None = None) -> None:
        redis = _require_redis()
        raw = url or os.getenv("REDIS_URL", "").strip()
        if not raw:
            raise RuntimeError("REDIS_URL is not set")
        self._client = redis.Redis.from_url(raw, decode_responses=True)

    def ping(self) -> bool:
        return bool(self._client.ping())

    def clear_v1(self) -> None:
        self._client.delete(seed_key_v1(), seed_meta_key())

    def write_top(
        self,
        ranked: list[tuple[str, float]],
        *,
        version: str = "v1",
        source: str = "bootstrap_mvp",
        redis_key: str | None = None,
    ) -> None:
        """``ranked`` is (address, score) descending desirability."""
        key = redis_key or (seed_key_v2() if version == "v2" else seed_key_v1())
        pipe = self._client.pipeline()
        pipe.delete(key)
        for addr, score in ranked[:500]:
            a = addr.lower().strip()
            if a.startswith("0x") and len(a) >= 42:
                pipe.zadd(key, {a[:42]: float(score)})
        pipe.hset(
            seed_meta_key(),
            mapping={
                "version": version,
                "source": source,
                "updated_ts": str(time.time()),
                "count": str(min(len(ranked), 500)),
            },
        )
        pipe.execute()

    def fetch_top(self, limit: int = 50, *, version: str = "v1") -> list[tuple[str, float]]:
        key = seed_key_v2() if version == "v2" else seed_key_v1()
        rows = self._client.zrevrange(key, 0, max(0, limit - 1), withscores=True)
        return [(str(a), float(s)) for a, s in rows]

    def member_set(self, *, version: str = "v1") -> set[str]:
        key = seed_key_v2() if version == "v2" else seed_key_v1()
        return {str(x).lower() for x in self._client.zrange(key, 0, -1)}

    def is_member(self, address: str, *, version: str = "v1") -> bool:
        key = seed_key_v2() if version == "v2" else seed_key_v1()
        return bool(self._client.zscore(key, address.lower()[:42]))

    def remember_cluster(self, entity_id: str, members: list[str], ttl_sec: int = 86400) -> None:
        """Optional: stash VirtualEntity members for cross-process inspection."""
        key = seed_cluster_key(entity_id)
        self._client.setex(key, ttl_sec, json.dumps([m.lower()[:42] for m in members]))
