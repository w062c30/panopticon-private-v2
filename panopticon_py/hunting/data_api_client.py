"""
panopticon_py/hunting/data_api_client.py
Data API client for Polymarket — /trades endpoint + timestamp normalization.
D169 P2-T3: Historical trades enrichment + safe_ts_to_seconds() validation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

PROCESS_VERSION = "v1.0.0-D169"

DATA_API_BASE = "https://data-api.polymarket.com"
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)

logger = logging.getLogger(__name__)


def safe_ts_to_seconds(ts: Any) -> int:
    """
    Normalize a Polymarket Data API timestamp to Unix seconds.

    IQ-2 resolution per D169 P2-T3 memo:
      - Confirmed: API returns Unix seconds (10-digit).
      - Heuristic kept for defense-in-depth against future API drift.
    """
    if not isinstance(ts, (int, float)):
        raise TypeError(f"timestamp must be int/float, got {type(ts).__name__}: {ts!r}")
    if ts > 1e12:
        return int(ts // 1000)
    if ts < 1e9:
        raise ValueError(f"timestamp {ts} below Unix-seconds range; suspected bad data")
    return int(ts)


class DataAPIClient:
    def __init__(self, base: str = DATA_API_BASE):
        self._base = base.rstrip("/")
        self._http: aiohttp.ClientSession | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._http

    async def fetch_user_trades(self, wallet: str, limit: int = 500) -> list[dict]:
        """Return list of trades with `timestamp` normalized to Unix seconds."""
        if not wallet or not wallet.startswith("0x"):
            raise ValueError(f"invalid wallet address: {wallet!r}")
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit out of range: {limit}")

        session = await self._ensure_http()
        url = f"{self._base}/trades"
        params = {"user": wallet, "limit": limit}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    logger.warning(
                        "[DATA_API] /trades wallet=%s status=%d",
                        wallet[:10],
                        resp.status,
                    )
                    return []
                data = await resp.json()
                if not isinstance(data, list):
                    logger.warning(
                        "[DATA_API] /trades returned non-list: %s",
                        type(data).__name__,
                    )
                    return []
                normalized: list[dict] = []
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    ts = row.get("timestamp")
                    if ts is None:
                        continue
                    try:
                        row["timestamp_seconds"] = safe_ts_to_seconds(ts)
                    except (TypeError, ValueError) as exc:
                        logger.debug("[DATA_API] bad ts %r: %s", ts, exc)
                        continue
                    normalized.append(row)
                return normalized
        except Exception as exc:
            logger.warning(
                "[DATA_API] /trades wallet=%s error=%s",
                wallet[:10],
                exc,
            )
            return []

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()