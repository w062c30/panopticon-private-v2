from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from panopticon_py.db import AsyncDBWriter
from panopticon_py.rate_limit_governor import RateLimitGovernor


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _moralis_base() -> str:
    try:
        p = Path(__file__).resolve().parents[2] / "config" / "api_capability_registry.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(
            data.get("apis", {})
            .get("moralis", {})
            .get("base_urls", {})
            .get("evm_api", "https://deep-index.moralis.io/api/v2.2")
        ).rstrip("/")
    except Exception:
        return "https://deep-index.moralis.io/api/v2.2"


def fetch_wallet_erc20_transfers(address: str, *, limit: int = 12, timeout_sec: float = 20.0) -> list[dict[str, object]]:
    key = os.getenv("MORALIS_API_KEY", "").strip()
    if not key:
        return []
    base = os.getenv("MORALIS_EVM_API_BASE", _moralis_base()).rstrip("/")
    url = f"{base}/{address}/erc20/transfers?chain=polygon&limit={limit}"
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": key, "Accept": "application/json", "User-Agent": "panopticon-ingestion/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        res = body.get("result")
        if isinstance(res, list):
            return [x for x in res if isinstance(x, dict)]
    return []


class MoralisIngestionWorker:
    """Optional: poll Moralis for watched wallet ERC20 transfer activity on Polygon."""

    def __init__(self, db, writer: AsyncDBWriter, governor: RateLimitGovernor | None = None, interval_sec: float | None = None) -> None:
        self.db = db
        self.writer = writer
        self.governor = governor or RateLimitGovernor()
        self.interval_sec = float(interval_sec if interval_sec is not None else os.getenv("MORALIS_POLL_INTERVAL_SEC", "45"))
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not os.getenv("MORALIS_API_KEY", "").strip():
            return
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while self._running:
            if not self.governor.allow("moralis_cu", 3.0):
                time.sleep(1.0)
                continue
            for addr in self.db.fetch_active_watched_addresses():
                if not self._running:
                    break
                if not self.governor.allow("moralis_cu", 2.0):
                    time.sleep(1.0)
                    break
                rows = fetch_wallet_erc20_transfers(addr)
                if not rows:
                    continue
                self.writer.submit(
                    "wallet_observation",
                    {
                        "obs_id": str(uuid4()),
                        "address": addr.lower(),
                        "market_id": None,
                        "obs_type": "moralis_tx",
                        "payload_json": {"transfers": rows[:12]},
                        "ingest_ts_utc": _utc_now(),
                    },
                )
            time.sleep(self.interval_sec)
