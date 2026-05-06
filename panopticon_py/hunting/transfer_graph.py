"""Transfer graph ingestion for D171 P4-T2 (revised, WSS-first).

Architecture rulings:
- WSS is the long-running primary path (CU ~= 0).
- HTTP eth_getLogs is cold-start one-shot only.
- Query window must align with pol_monitor MAX_BLOCKS_PER_LOGS_QUERY (9).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable

import aiohttp

from panopticon_py.db import DBWriterQueue
from panopticon_py.hunting.entity_linker import EntityLinker
from panopticon_py.hunting.pol_monitor import (
    ALCHEMY_HTTP_URL,
    MAX_BLOCKS_PER_LOGS_QUERY,
    TRANSFER_TOPIC,
    USDC_DECIMALS,
    USDC_E_ADDRESS,
)
from panopticon_py.time_utils import utc_now_rfc3339_ms

PROCESS_VERSION = "v1.1.0-D171"

MAX_BLOCKS_PER_HOP = MAX_BLOCKS_PER_LOGS_QUERY
MAX_NODES_PER_GRAPH = 200
PER_HOP_TIMEOUT_SEC = 10.0
INTER_REQUEST_SLEEP_SEC = float(os.getenv("TG_INTER_REQ_SLEEP", "3.0"))
RATE_LIMIT_BACKOFF_SEC = float(os.getenv("TG_RATE_LIMIT_BACKOFF", "120.0"))
BATCH_WALLETS_PER_CYCLE = int(os.getenv("TG_BATCH_WALLETS", "5"))
COLD_START_LOOKBACK_HOURS = float(os.getenv("TG_COLD_START_LOOKBACK_HOURS", "24.0"))
SAFE_FALLBACK_BLOCK = 86_400_000

logger = logging.getLogger(__name__)


def _wallet_to_topic(wallet: str) -> str:
    addr = wallet.lower().removeprefix("0x")
    return "0x" + addr.rjust(64, "0")


class _RateLimitExceeded(Exception):
    pass


def _decode_log(log: dict) -> dict | None:
    topics = log.get("topics") or []
    if len(topics) < 3:
        return None
    try:
        from_addr = ("0x" + topics[1][-40:]).lower()
        to_addr = ("0x" + topics[2][-40:]).lower()
        raw = int(log.get("data", "0x0"), 16)
        usdc = raw / (10 ** USDC_DECIMALS)
        block = int(str(log.get("blockNumber", "0x0")), 16)
        tx_hash = str(log.get("transactionHash", ""))
    except (ValueError, TypeError, KeyError):
        return None
    return {
        "from": from_addr,
        "to": to_addr,
        "usdc_amount": usdc,
        "block": block,
        "tx_hash": tx_hash,
    }


def _persist_logs(logs: list[dict], root_wallet: str, hop_depth: int, linker: EntityLinker) -> None:
    ts_utc = utc_now_rfc3339_ms()
    label_to_score = {
        "ANONYMIZER": 0.0,
        "DEX_ROUTER": 0.5,
        "CEX": 1.0,
        "EOA_PERSONAL": 1.0,
        "UNKNOWN": 0.3,
    }
    for log in logs:
        t = _decode_log(log)
        if not t or t["usdc_amount"] <= 0:
            continue
        label, source, confidence = linker.classify(t["from"])
        linker.persist_label(t["from"], label, source, confidence)
        DBWriterQueue.put(
            """
            INSERT OR IGNORE INTO transfer_graph
              (root_wallet, from_addr, to_addr, usdc_amount, block, hop_depth,
               tx_hash, created_ts_utc, wallet_address, entity_link_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_wallet.lower(),
                t["from"],
                t["to"],
                t["usdc_amount"],
                t["block"],
                hop_depth,
                t["tx_hash"],
                ts_utc,
                root_wallet.lower(),
                label_to_score.get(label, 0.3),
            ),
            table_hint="transfer_graph",
        )


async def _cold_start_fetch(
    wallet: str,
    latest_block: int,
    api_key: str,
    session: aiohttp.ClientSession,
) -> list[dict]:
    """Single request for latest 9 blocks only."""
    from_block = max(0, latest_block - MAX_BLOCKS_PER_HOP + 1)
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "eth_getLogs",
        "params": [{
            "fromBlock": hex(from_block),
            "toBlock": hex(latest_block),
            "address": USDC_E_ADDRESS,
            "topics": [TRANSFER_TOPIC, None, _wallet_to_topic(wallet)],
        }],
    }
    url = ALCHEMY_HTTP_URL.format(api_key=api_key)
    async with session.post(url, json=payload) as resp:
        if resp.status in (429, 403):
            raise _RateLimitExceeded(f"HTTP {resp.status}")
        try:
            data = await resp.json(content_type=None)
        except Exception as exc:
            body = await resp.text()
            logger.warning(
                "[TG][COLD_FETCH_ERR] wallet=%s status=%s exc=%s body=%r",
                wallet[:10],
                resp.status,
                exc,
                body[:160],
            )
            return []
    if not isinstance(data, dict):
        return []
    if data.get("error"):
        logger.warning("[TG][COLD_RPC_ERR] wallet=%s err=%s", wallet[:10], data.get("error"))
        return []
    logs = data.get("result")
    return logs if isinstance(logs, list) else []


async def init_transfer_graph(
    watchlist: list[str],
    alchemy_key: str,
    linker: EntityLinker,
    get_latest_block_fn: Callable[[], Any],
    added_after_epoch: float | None = None,
) -> tuple[int, int, int]:
    """
    One-shot cold-start task; exits naturally when done.

    Returns (wallets_attempted, wallets_completed, rate_limit_count).
    Caller writes CU report from these values.

    D171 Q1 Option B ruling: cold-start lookback uses first_seen_ts_utc
    (written by WhaleScanner on first INSERT), NOT added_ts_utc.
    Semantics are equivalent — first_seen IS the add event.
    Caller (_recently_added_wallets) applies the first_seen_ts_utc filter
    before passing watchlist here. This function receives a pre-filtered list.

    added_after_epoch: informational only (not used inside this function).
    """
    if not watchlist:
        logger.info("[TG] cold-start: watchlist empty")
        return 0, 0, 0

    total = len(watchlist)
    wallets_attempted = 0
    wallets_completed = 0
    rate_limit_count = 0
    consecutive_rate_limits = 0

    timeout = aiohttp.ClientTimeout(total=PER_HOP_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        latest_block = int(await get_latest_block_fn())
        if latest_block < 1_000_000:
            logger.error(
                "[TG] cold-start aborted: latest_block=%d looks invalid",
                latest_block,
            )
            return 0, 0, 0

        logger.info(
            "[TG] cold-start begin wallets=%d latest_block=%d lookback=%.0fh",
            total,
            latest_block,
            COLD_START_LOOKBACK_HOURS,
        )

        for i in range(0, total, max(1, BATCH_WALLETS_PER_CYCLE)):
            batch = watchlist[i:i + max(1, BATCH_WALLETS_PER_CYCLE)]
            for wallet in batch:
                wallets_attempted += 1
                try:
                    logs = await _cold_start_fetch(wallet, latest_block, alchemy_key, session)
                    _persist_logs(logs, wallet, hop_depth=1, linker=linker)
                    wallets_completed += 1
                    consecutive_rate_limits = 0
                except _RateLimitExceeded:
                    rate_limit_count += 1
                    consecutive_rate_limits += 1
                    backoff = RATE_LIMIT_BACKOFF_SEC * min(consecutive_rate_limits, 5)
                    logger.warning(
                        "[TG] cold-start rate-limited (consecutive=%d, total=%d); "
                        "backoff %.0fs",
                        consecutive_rate_limits,
                        rate_limit_count,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    if consecutive_rate_limits >= 2:
                        try:
                            latest_block = int(await get_latest_block_fn())
                        except Exception:
                            pass
                    continue
                await asyncio.sleep(INTER_REQUEST_SLEEP_SEC)

    logger.info(
        "[TG] cold-start complete attempted=%d completed=%d rate_limits=%d",
        wallets_attempted,
        wallets_completed,
        rate_limit_count,
    )
    return wallets_attempted, wallets_completed, rate_limit_count


async def _write_cu_report(
    wallet_count_attempted: int,
    wallet_count_completed: int,
    rate_limit_count: int,
) -> None:
    cu_per_req = 75
    total_cu = wallet_count_completed * cu_per_req
    report = f"""# D171 Alchemy CU Report (P4-T2 Revised — Runtime)

## Cold-start Execution Summary
- Wallets attempted : {wallet_count_attempted}
- Wallets completed : {wallet_count_completed}
- Rate limit events : {rate_limit_count}
- CU consumed       : {total_cu:,} ({wallet_count_completed} × {cu_per_req} CU)
- Free-tier budget  : 300,000,000 CU/month (10,000,000 CU/day)

## Ongoing (WSS)
- CU per event      : ~0 (eth_subscribe, not counted in request CU)

## Notes
- TG_BATCH_WALLETS             = {BATCH_WALLETS_PER_CYCLE}
- TG_INTER_REQ_SLEEP           = {INTER_REQUEST_SLEEP_SEC}
- TG_RATE_LIMIT_BACKOFF        = {RATE_LIMIT_BACKOFF_SEC}
- TG_COLD_START_LOOKBACK_HOURS = {COLD_START_LOOKBACK_HOURS}
"""
    path = "temp_architect_handoffs/d171_alchemy_cu_report.md"
    import os
    os.makedirs("temp_architect_handoffs", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info("[TG] CU report written: %s", path)


class TransferGraphIngester:
    """WSS-driven transfer graph ingester (long-running)."""

    def __init__(self, linker: EntityLinker, watchlist_fn: Callable[[], set[str]]) -> None:
        self._linker = linker
        self._watchlist_fn = watchlist_fn
        self._watchlist_cache: set[str] = set()
        self._watchlist_cache_ts: float = 0.0
        self._WATCHLIST_CACHE_TTL = 60.0  # seconds — rebuild set every 60s
        self._events_seen = 0

    def _get_watchlist(self) -> set[str]:
        """Cached watchlist with 60s TTL to avoid per-event DB queries."""
        now = time.monotonic()
        if now - self._watchlist_cache_ts > self._WATCHLIST_CACHE_TTL:
            self._watchlist_cache = self._watchlist_fn()
            self._watchlist_cache_ts = now
        return self._watchlist_cache

    async def run(self, tg_ingest_queue: asyncio.Queue) -> None:
        logger.info("[TGI] ingester started")
        while True:
            try:
                event: dict = await asyncio.wait_for(tg_ingest_queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._handle_event(event)
            except Exception as exc:
                logger.exception("[TGI] handle_event failed: %s", exc)
            finally:
                tg_ingest_queue.task_done()

    async def _handle_event(self, event: dict) -> None:
        to_addr = str(event.get("to") or "").lower()
        if to_addr not in self._get_watchlist():
            return
        from_addr = str(event.get("from") or "").lower()
        usdc_amount = float(event.get("usdc_amount") or 0.0)
        block = int(event.get("block") or 0)
        tx_hash = str(event.get("tx_hash") or "")
        if usdc_amount <= 0:
            return
        label, source, confidence = self._linker.classify(from_addr)
        self._linker.persist_label(from_addr, label, source, confidence)
        label_to_score = {
            "ANONYMIZER": 0.0,
            "DEX_ROUTER": 0.5,
            "CEX": 1.0,
            "EOA_PERSONAL": 1.0,
            "UNKNOWN": 0.3,
        }
        DBWriterQueue.put(
            """
            INSERT OR IGNORE INTO transfer_graph
              (root_wallet, from_addr, to_addr, usdc_amount, block, hop_depth,
               tx_hash, created_ts_utc, wallet_address, entity_link_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                to_addr,
                from_addr,
                to_addr,
                usdc_amount,
                block,
                1,
                tx_hash,
                utc_now_rfc3339_ms(),
                to_addr,
                label_to_score.get(label, 0.3),
            ),
            table_hint="transfer_graph",
        )
        self._events_seen += 1
        if self._events_seen % 100 == 0:
            logger.info("[TGI] events_seen=%d", self._events_seen)


def fund_source_score_from_graph(root_wallet: str, linker: EntityLinker, db_conn) -> float:
    """Compute weighted fund source score for w4."""
    rows = db_conn.execute(
        "SELECT from_addr, usdc_amount FROM transfer_graph WHERE root_wallet = ? AND hop_depth = 1",
        (root_wallet.lower(),),
    ).fetchall()
    if not rows:
        return 0.0
    total_usdc = sum(float(r["usdc_amount"]) for r in rows)
    if total_usdc <= 0:
        return 0.0
    weights = {
        "ANONYMIZER": 0.0,
        "DEX_ROUTER": 0.5,
        "CEX": 1.0,
        "EOA_PERSONAL": 1.0,
        "UNKNOWN": 0.3,
    }
    weighted = 0.0
    for r in rows:
        label, _, _ = linker.classify(str(r["from_addr"]))
        weighted += weights.get(label, 0.3) * float(r["usdc_amount"])
    return max(0.0, min(1.0, weighted / total_usdc))