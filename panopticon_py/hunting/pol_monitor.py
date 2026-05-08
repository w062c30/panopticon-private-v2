"""
T2-POL Political Market Monitor (D101)
======================================
Scans Gamma API for political-market slugs and maintains pol_market_watchlist.
Does NOT generate SignalEvents — political market signals are triggered by
the standard T2 radar path via wallet_observations (Invariant 1.4).

Design constraints:
- pol_monitor.py ONLY maintains the watchlist (read + write to pol_market_watchlist)
- Signal generation is handled by run_radar's entropy window + signal_engine pipeline
- All Gamma API field access uses .get() — no direct [] indexing (RULE-API-1)

Invariant 1.4: T2/T2-POL Smart Money signal source is wallet_observations, not OFI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import urllib.parse
from typing import Any

import aiohttp
import websockets

from panopticon_py.db import DBWriterQueue
from panopticon_py.db import ShadowDB
from panopticon_py.time_utils import utc_now_rfc3339_ms

logger = logging.getLogger(__name__)

_PROCESS_VERSION = "v1.0.0-D169"
_POL_WS_BACKOFF_BASE = float(os.getenv("POL_WS_BACKOFF_BASE_SEC", "60.0"))
_POL_WS_BACKOFF_MAX = float(os.getenv("POL_WS_BACKOFF_MAX_SEC", "900.0"))
_pol_ws_consecutive_failures = 0

# Political market keyword whitelist (slug match, lowercase)
POL_KEYWORDS: list[str] = [
    "trump", "biden", "harris", "election", "congress", "senate",
    "president", "impeach", "tariff", "fed-chair", "supreme-court",
    "nato", "war", "ceasefire", "sanction", "debt-ceiling",
    "legislation", "veto", "executive-order",
]

POL_CATEGORY_MAP: dict[str, str] = {
    "election": "ELECTION", "president": "ELECTION", "senate": "ELECTION",
    "congress": "LEGISLATION", "legislation": "LEGISLATION", "veto": "LEGISLATION",
    "tariff": "POLICY", "fed-chair": "APPOINTMENT", "supreme-court": "APPOINTMENT",
    "war": "GEOPOLITICAL", "ceasefire": "GEOPOLITICAL", "nato": "GEOPOLITICAL",
    "sanction": "GEOPOLITICAL",
}

# Slug segments that indicate non-political or wrong category
_EXCLUDE_SLUG_SEGMENTS: list[str] = [
    "updown", "5m", "sport", "nba", "nfl", "nhl", "soccer",
    "champion", "winner", "world-cup", "playoff", "season",
]

GAMMA_URL = "https://gamma-api.polymarket.com/markets"

USDC_E_ADDRESS = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ALCHEMY_WSS_URL = "wss://polygon-mainnet.g.alchemy.com/v2/{api_key}"
ALCHEMY_HTTP_URL = "https://polygon-mainnet.g.alchemy.com/v2/{api_key}"
USDC_DECIMALS = 6
MIN_USDC_FILTER = float(os.getenv("PANOPTICON_MIN_USDC_FILTER", "100.0"))
MAX_BLOCKS_PER_LOGS_QUERY = 9
REORG_BUFFER = 5

# D101: Concurrency guard for Gamma API calls
# Note: semaphore is now lazily initialised inside scan_pol_markets()
# to avoid "attached to a different loop" errors at import time.


def _extract_token_ids(m: dict) -> tuple[str | None, str | None]:
    """
    D111: Multi-strategy token_id extraction from Gamma API market dict.
    Returns (token_id_yes, token_id_no) — YES is clob[0], NO is clob[1].

    Both may be None if API response has no token fields.
    RULE-API-1 compliant: all access via .get(), isinstance guards.
    """
    yes_id: str | None = None
    no_id:  str | None = None

    # Strategy 1: tokens[] object array
    tokens = m.get("tokens") or []
    if len(tokens) >= 1 and isinstance(tokens[0], dict):
        yes_id = tokens[0].get("token_id") or tokens[0].get("tokenId") or None
    if len(tokens) >= 2 and isinstance(tokens[1], dict):
        no_id  = tokens[1].get("token_id") or tokens[1].get("tokenId") or None
    if yes_id:
        return str(yes_id), (str(no_id) if no_id else None)

    # Strategy 2: clobTokenIds (JSON-encoded string or list)
    clob = m.get("clobTokenIds")
    if isinstance(clob, str):
        try:
            clob = json.loads(clob)
        except Exception:
            clob = []
    if isinstance(clob, list):
        if len(clob) >= 1 and clob[0]:
            yes_id = str(clob[0])
        if len(clob) >= 2 and clob[1]:
            no_id = str(clob[1])
    if yes_id:
        return yes_id, no_id

    # Strategy 3: direct field (YES only — no NO equivalent)
    t = m.get("tokenId") or m.get("token_id")
    return (str(t) if t else None), None


def _process_market_record(
    m: dict,
    pending: list[dict],
    upserted_ids: set[str],
) -> bool:
    """
    D104: No longer writes to DB directly.
    Appends to `pending` list for bulk upsert by caller.
    Returns True if filtered-in, False if filtered.
    """
    slug = (m.get("slug") or "").lower()
    if any(seg in slug for seg in _EXCLUDE_SLUG_SEGMENTS):
        return False
    try:
        # D110: use volume24hr (rolling 24h) instead of volume (all-time cumulative)
        # Fall back to volumeNum, then volume — matching _refresh_tier2_tokens strategy
        vol = float(
            m.get("volume24hr")
            if m.get("volume24hr") is not None
            else (m.get("volumeNum") or m.get("volume") or 0)
        )
        best_bid = float(m.get("bestBid") or 0.5)
    except (ValueError, TypeError):
        return False
    if vol < 5000 or best_bid >= 0.99 or best_bid <= 0.01:
        logger.debug(
            "[POL_FILTER] rejected slug=%s vol=%.0f vol24h=%s bid=%.3f",
            slug[:40],
            vol,
            m.get("volume24hr"),
            best_bid,
        )
        return False
    matched_kw = [kw for kw in POL_KEYWORDS if kw in slug]
    if not matched_kw:
        logger.debug("[POL_FILTER] no_keyword_match slug=%s", slug[:40])
        return False
    market_id = m.get("conditionId") or m.get("id") or ""
    if not market_id:
        return False
    category = next(
        (POL_CATEGORY_MAP[kw] for kw in matched_kw if kw in POL_CATEGORY_MAP),
        "OTHER",
    )
    token_id_yes, token_id_no = _extract_token_ids(m)
    pending.append({
        "market_id":          market_id,
        "token_id":          token_id_yes,
        "token_id_no":       token_id_no,
        "event_slug":         slug,
        "political_category": category,
        "entity_keywords":    matched_kw,
        "subscribed_at":      utc_now_rfc3339_ms(),
    })
    upserted_ids.add(market_id)
    return True


async def scan_pol_markets(db: ShadowDB, *, max_pages: int = 5) -> int:
    """
    Scan Gamma API for political markets matching POL_KEYWORDS.
    Upserts matching markets into pol_market_watchlist.

    Returns the count of upserted/updated markets.

    Filter criteria (mirrors Invariant 1.4 T2 definition):
    - active=True, closed=False, archived=False
    - Exclude updown/5m/sports slug segments
    - bestBid NOT in [0.99, ∞) or (-∞, 0.01]
    - volume >= 5000
    - slug contains at least one POL_KEYWORD
    """
    try:
        import httpx
    except ImportError:
        logger.warning("[POL_SCAN] httpx not installed, skipping scan")
        return 0

    count = 0
    total_from_api = 0
    offset = 0
    limit = 100
    upserted_ids: set[str] = set()
    pending: list[dict] = []  # D104: bulk upsert buffer

    # D102: Lazy semaphore — avoid "attached to a different loop" at import time
    semaphore = asyncio.Semaphore(2)

    async with httpx.AsyncClient(timeout=10.0) as client:
        for _ in range(max_pages):
            # D102: Semaphore wraps single request, not entire loop + sleep
            async with semaphore:
                try:
                    resp = await client.get(
                        GAMMA_URL,
                        params={
                            "active": "true",
                            "closed": "false",
                            "archived": "false",
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                    resp.raise_for_status()
                    markets = resp.json()
                except Exception as exc:
                    logger.warning("[POL_SCAN] gamma-api error: %s", exc)
                    # D103: first-page failure — skip deactivation to preserve existing records
                    if not upserted_ids:
                        logger.warning(
                            "[POL_SCAN] first-page failure — deactivation skipped "
                            "to avoid false-deactivation. All existing watchlist markets preserved as active."
                        )
                    break

            if not markets:
                break

            total_from_api += len(markets)
            for m in markets:
                if _process_market_record(m, pending, upserted_ids):
                    count += 1

            if len(markets) < limit:
                break
            offset += limit
            await asyncio.sleep(0.5)  # rate limit guard — outside semaphore

    # D104: Bulk upsert all pending records in single transaction
    if pending:
        db.bulk_upsert_pol_markets(pending)

    # D102-2: Deactivate markets not seen in this scan
    if upserted_ids:
        db.deactivate_closed_pol_markets(upserted_ids)

    # D104: Correct three-way distinction
    if total_from_api == 0:
        logger.warning(
            "[POL_SCAN] Zero markets returned from API — may be unreachable or empty. "
            "Existing watchlist preserved."
        )
    elif count == 0:
        logger.warning(
            "[POL_SCAN] API returned %d markets but NONE passed filters "
            "(vol>=5000 / bestBid / POL_KEYWORDS). "
            "Keywords: %s",
            total_from_api, POL_KEYWORDS,
        )
    else:
        logger.info("[POL_SCAN] upserted=%d / api_total=%d political markets", count, total_from_api)

    return count


def sync_scan_pol_markets(db: ShadowDB, *, max_pages: int = 5) -> int:
    """
    Synchronous wrapper for scan_pol_markets.
    Used when calling from a thread-based async context (e.g. asyncio.to_thread).
    """
    try:
        import httpx
    except ImportError:
        logger.warning("[POL_SCAN] httpx not installed, skipping scan")
        return 0

    count = 0
    total_from_api = 0
    offset = 0
    limit = 100
    upserted_ids: set[str] = set()
    pending: list[dict] = []  # D104: bulk upsert buffer

    for _ in range(max_pages):
        try:
            resp = httpx.get(
                GAMMA_URL,
                params={
                    "active": "true",
                    "closed": "false",
                    "archived": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            markets = resp.json()
        except Exception as exc:
            logger.warning("[POL_SCAN] gamma-api (sync) error: %s", exc)
            # D103: first-page failure — skip deactivation to preserve existing records
            if not upserted_ids:
                logger.warning(
                    "[POL_SCAN][SYNC] first-page failure — deactivation skipped "
                    "to avoid false-deactivation. All existing watchlist markets preserved as active."
                )
            break

        if not markets:
            break

        total_from_api += len(markets)
        for m in markets:
            if _process_market_record(m, pending, upserted_ids):
                count += 1

        if len(markets) < limit:
            break
        offset += limit

    # D104: Bulk upsert all pending records in single transaction
    if pending:
        db.bulk_upsert_pol_markets(pending)

    # D102-2: Deactivate markets not seen in this scan
    if upserted_ids:
        db.deactivate_closed_pol_markets(upserted_ids)

    # D104: Correct three-way distinction
    if total_from_api == 0:
        logger.warning(
            "[POL_SCAN][SYNC] Zero markets returned from API — may be unreachable or empty. "
            "Existing watchlist preserved."
        )
    elif count == 0:
        logger.warning(
            "[POL_SCAN][SYNC] API returned %d markets but NONE passed filters "
            "(vol>=5000 / bestBid / POL_KEYWORDS). "
            "Keywords: %s",
            total_from_api, POL_KEYWORDS,
        )
    else:
        logger.info("[POL_SCAN][SYNC] upserted=%d / api_total=%d political markets", count, total_from_api)

    return count


class PolygonListener:
    def __init__(self, api_key: str, outbound: asyncio.Queue, db_path: str) -> None:
        self._api_key = api_key
        self._outbound = outbound
        self._db_path = db_path
        self._http_session: aiohttp.ClientSession | None = None
        self._last_block = self._load_last_block()

    def _load_last_block(self) -> int:
        try:
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                row = conn.execute(
                    "SELECT last_processed_block FROM polygon_sync WHERE id=1"
                ).fetchone()
                return int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as exc:
            logger.warning("[POL_LISTENER] load last block failed: %s", exc)
            return 0

    def _save_last_block(self, block: int) -> None:
        ok = DBWriterQueue.put(
            "INSERT OR REPLACE INTO polygon_sync (id, last_processed_block, updated_ts_utc) VALUES (1, ?, ?)",
            (int(block), utc_now_rfc3339_ms()),
            table_hint="polygon_sync",
        )
        if not ok:
            logger.warning("[POL_LISTENER] failed to enqueue polygon_sync update block=%s", block)

    def _decode_transfer(self, log: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(log, dict):
            return None
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 3:
            return None
        if str(topics[0]).lower() != TRANSFER_TOPIC:
            return None
        try:
            from_addr = ("0x" + str(topics[1])[-40:]).lower()
            to_addr = ("0x" + str(topics[2])[-40:]).lower()
            raw = int(str(log.get("data", "0x0")), 16)
            block_num = int(str(log.get("blockNumber", "0x0")), 16)
            log_index = int(str(log.get("logIndex", "0x0")), 16)
        except (TypeError, ValueError):
            return None

        usdc_amount = raw / (10 ** USDC_DECIMALS)
        if usdc_amount < MIN_USDC_FILTER:
            return None

        tx_hash = str(log.get("transactionHash") or "").lower()
        return {
            "tx_hash": tx_hash,
            "block": block_num,
            "from": from_addr,
            "to": to_addr,
            "usdc_amount": usdc_amount,
            "log_index": log_index,
            "received_ts_utc": utc_now_rfc3339_ms(),
        }

    def _put_safe(self, item: dict[str, Any]) -> None:
        try:
            self._outbound.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning(
                "[POL_LISTENER] outbound queue full, dropping tx=%s",
                str(item.get("tx_hash", ""))[:18],
            )

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._http_session

    async def _http_post_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = await self._ensure_http_session()
        async with session.post(ALCHEMY_HTTP_URL.format(api_key=self._api_key), json=payload) as resp:
            return await resp.json()

    async def _get_latest_block(self) -> int:
        data = await self._http_post_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
        )
        result = data.get("result")
        if not isinstance(result, str):
            raise RuntimeError(f"eth_blockNumber malformed response: {data}")
        return int(result, 16)

    async def _http_fallback(self) -> None:
        try:
            latest = await self._get_latest_block() - REORG_BUFFER
        except Exception as exc:
            logger.warning("[POL_LISTENER] fallback latest-block error: %s", exc)
            return

        start = max(self._last_block + 1, latest - 100)
        if start > latest:
            return

        logger.info("[POL_LISTENER] fallback start=%d latest=%d", start, latest)
        while start <= latest:
            end = min(start + MAX_BLOCKS_PER_LOGS_QUERY - 1, latest)
            try:
                data = await self._http_post_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "eth_getLogs",
                        "params": [
                            {
                                "fromBlock": hex(start),
                                "toBlock": hex(end),
                                "address": USDC_E_ADDRESS,
                                "topics": [TRANSFER_TOPIC],
                            }
                        ],
                    }
                )
                logs = data.get("result")
                if not isinstance(logs, list):
                    logger.warning("[POL_LISTENER] eth_getLogs malformed response: %s", data)
                    return
                pushed = 0
                for raw_log in logs:
                    item = self._decode_transfer(raw_log)
                    if item:
                        self._put_safe(item)
                        pushed += 1
                self._last_block = end
                self._save_last_block(end)
                logger.info(
                    "[POL_LISTENER] eth_getLogs blocks=%d-%d items=%d",
                    start,
                    end,
                    pushed,
                )
            except Exception as exc:
                logger.warning("[POL_LISTENER] eth_getLogs blocks=%d-%d error=%s", start, end, exc)
                return
            start = end + 1
            await asyncio.sleep(0.2)

    async def _wss_loop(self) -> None:
        global _pol_ws_consecutive_failures  # D179b: must declare or += in except raises UnboundLocalError
        backoff = 5.0
        while True:
            try:
                async with websockets.connect(
                    ALCHEMY_WSS_URL.format(api_key=self._api_key),
                    ping_interval=30,
                    ping_timeout=15,
                    subprotocols=None,
                    extensions=None,
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "eth_subscribe",
                                "params": [
                                    "logs",
                                    {"address": USDC_E_ADDRESS, "topics": [TRANSFER_TOPIC]},
                                ],
                            }
                        )
                    )
                    sub_resp = json.loads(await ws.recv())
                    sub_id = sub_resp.get("result")
                    if not isinstance(sub_id, str):
                        logger.error("[POL_LISTENER] eth_subscribe failed: %s", sub_resp)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _POL_WS_BACKOFF_MAX)
                        continue
                    logger.info("[POL_LISTENER] WSS subscribed sub_id=%s", sub_id)
                    backoff = 5.0
                    _pol_ws_consecutive_failures = 0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("method") != "eth_subscription":
                            continue
                        log = (msg.get("params") or {}).get("result")
                        item = self._decode_transfer(log)
                        if item:
                            self._put_safe(item)
                            self._last_block = int(item["block"])
                            self._save_last_block(self._last_block)
            except Exception as exc:
                _pol_ws_consecutive_failures += 1
                backoff = min(
                    _POL_WS_BACKOFF_BASE * (2 ** (_pol_ws_consecutive_failures - 1)),
                    _POL_WS_BACKOFF_MAX,
                )
                logger.warning(
                    "[POL_LISTENER][D176] WSS error: %s — backoff=%.0fs (failure #%d)",
                    exc, backoff, _pol_ws_consecutive_failures,
                )
                await self._http_fallback()
                await asyncio.sleep(backoff)

    async def run(self) -> None:
        if not self._api_key:
            logger.error("[POL_LISTENER] ALCHEMY_API_KEY missing; listener disabled")
            return
        logger.info(
            "[POL_LISTENER] starting version=%s last_block=%d",
            _PROCESS_VERSION,
            self._last_block,
        )
        try:
            await self._wss_loop()
        finally:
            if self._http_session and not self._http_session.closed:
                with contextlib.suppress(Exception):
                    await self._http_session.close()