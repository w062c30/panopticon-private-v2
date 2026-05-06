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
INTER_REQUEST_SLEEP_SEC = float(os.getenv("TG_INTER_REQ_SLEEP", "1.0"))
RATE_LIMIT_BACKOFF_SEC = float(os.getenv("TG_RATE_LIMIT_BACKOFF", "120.0"))
BATCH_WALLETS_PER_CYCLE = int(os.getenv("TG_BATCH_WALLETS", "50"))

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
) -> None:
    """One-shot cold-start task; exits naturally once done."""
    if not watchlist:
        logger.info("[TG] cold-start: watchlist empty")
        return
    timeout = aiohttp.ClientTimeout(total=PER_HOP_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        latest_block = int(await get_latest_block_fn())
        logger.info("[TG] cold-start begin wallets=%d latest_block=%d", len(watchlist), latest_block)
        for i in range(0, len(watchlist), max(1, BATCH_WALLETS_PER_CYCLE)):
            batch = watchlist[i:i + max(1, BATCH_WALLETS_PER_CYCLE)]
            for wallet in batch:
                try:
                    logs = await _cold_start_fetch(wallet, latest_block, alchemy_key, session)
                    _persist_logs(logs, wallet, hop_depth=1, linker=linker)
                except _RateLimitExceeded:
                    logger.warning("[TG] cold-start rate-limited; backoff %.0fs", RATE_LIMIT_BACKOFF_SEC)
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_SEC)
                await asyncio.sleep(INTER_REQUEST_SLEEP_SEC)
    logger.info("[TG] cold-start complete wallets=%d", len(watchlist))


class TransferGraphIngester:
    """WSS-driven transfer graph ingester (long-running)."""

    def __init__(self, linker: EntityLinker, watchlist_fn: Callable[[], set[str]]) -> None:
        self._linker = linker
        self._watchlist_fn = watchlist_fn
        self._events_seen = 0

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
        if to_addr not in self._watchlist_fn():
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