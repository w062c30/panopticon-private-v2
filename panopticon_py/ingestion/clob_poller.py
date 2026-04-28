from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from panopticon_py.db import AsyncDBWriter
from panopticon_py.ingestion.clob_client import fetch_book, fetch_trades
from panopticon_py.rate_limit_governor import RateLimitGovernor


def extract_addresses_from_trade(tr: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def push(v: Any) -> None:
        if isinstance(v, str) and v.startswith("0x") and len(v) >= 42:
            out.append(v.lower()[:42])

    for k in ("maker_address", "taker_address", "maker", "taker", "owner", "proxyWallet", "address"):
        push(tr.get(k))
    u = tr.get("user") or tr.get("trader")
    if isinstance(u, dict):
        push(u.get("address"))
    elif isinstance(u, str):
        push(u)
    return list(dict.fromkeys(out))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def token_ids_from_env() -> list[str]:
    raw = os.getenv("PANOPTICON_CLOB_TOKEN_IDS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    one = os.getenv("PANOPTICON_CLOB_TOKEN_ID", "").strip()
    return [one] if one else []


class ClobIngestionWorker:
    """Poll CLOB book + trades; enqueue wallet_observation rows (non-blocking via AsyncDBWriter)."""

    def __init__(
        self,
        writer: AsyncDBWriter,
        governor: RateLimitGovernor | None = None,
        interval_sec: float | None = None,
    ) -> None:
        self.writer = writer
        self.governor = governor or RateLimitGovernor()
        self.interval_sec = float(interval_sec if interval_sec is not None else os.getenv("CLOB_POLL_INTERVAL_SEC", "10"))
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
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
        tokens = token_ids_from_env()
        if not tokens:
            while self._running:
                time.sleep(self.interval_sec)
            return
        while self._running:
            for tid in tokens:
                if not self._running:
                    break
                if not self.governor.allow("polymarket_book"):
                    time.sleep(0.5)
                    continue
                ts = _utc_now()
                book = fetch_book(tid)
                if book is not None:
                    self.writer.submit(
                        "wallet_observation",
                        {
                            "obs_id": str(uuid4()),
                            "address": "0x0000000000000000000000000000000000000000",
                            "market_id": tid,
                            "obs_type": "clob_book",
                            "payload_json": {"token_id": tid, "book": book},
                            "ingest_ts_utc": ts,
                        },
                    )
                if not self.governor.allow("polymarket_book"):
                    time.sleep(0.5)
                    continue
                trades = fetch_trades(tid)
                for tr in trades:
                    addrs = extract_addresses_from_trade(tr)
                    if not addrs:
                        continue
                    for addr in addrs:
                        self.writer.submit(
                            "wallet_observation",
                            {
                                "obs_id": str(uuid4()),
                                "address": addr,
                                "market_id": tid,
                                "obs_type": "clob_trade",
                                "payload_json": {"token_id": tid, "trade": tr},
                                "ingest_ts_utc": _utc_now(),
                            },
                        )
            time.sleep(self.interval_sec)
