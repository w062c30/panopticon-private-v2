"""Transfer Graph Builder — D171 Phase 4 P4-T2.

Builds directed graphs of USDC.e fund flows per watchlisted wallet.
Persists edges in `transfer_graph` table; classifies entities via
`entity_labels` table using `config/cex_dex_routers_blacklist.json`.

Rules:
  - R-7: graph analysis outputs ONLY feed insider_score.
    Never consumed by Graphify, decision, or execution paths.
  - HARD CAPS: max_hops=2, max_blocks_per_hop=5000, max_nodes_per_graph=200.
  - CEX_ANONYMIZED: hop hits blacklist → weight=0, fall back on 4D + shadow PnL.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp

from panopticon_py.db import DBWriterQueue
from panopticon_py.hunting.pol_monitor import (
    ALCHEMY_HTTP_URL,
    TRANSFER_TOPIC,
    USDC_DECIMALS,
    USDC_E_ADDRESS,
)
from panopticon_py.time_utils import utc_now_rfc3339_ms

PROCESS_VERSION = "v1.0.0-D171"

MAX_HOPS_DEFAULT      = 2
MAX_BLOCKS_PER_HOP   = 5000
MAX_NODES_PER_GRAPH  = 200
ETH_GETLOGS_PAGE_SIZE = 9
PER_HOP_TIMEOUT_SEC  = 60.0

logger = logging.getLogger(__name__)


def _wallet_to_topic(wallet: str) -> str:
    addr = wallet.lower().removeprefix("0x")
    return "0x" + addr.rjust(64, "0")


class EntityLinker:
    """Classify wallet addresses using config/cex_dex_routers_blacklist.json."""

    def __init__(self, blacklist_path: str | None = None) -> None:
        self._anonymizers: set[str] = set()
        self._cex: set[str] = set()
        self._dex: set[str] = set()
        self._load(blacklist_path)

    def _load(self, path: str | None) -> None:
        import pathlib
        p = pathlib.Path(path) if path else None
        if p is None or not p.is_file():
            root = pathlib.Path(__file__).resolve().parents[2]
            p = root / "config" / "cex_dex_routers_blacklist.json"
        if not p.is_file():
            logger.warning("[ENTITY] blacklist not found at %s — running with empty sets", p)
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error("[ENTITY] blacklist parse error: %s", exc)
            return
        addrs = data.get("addresses", []) if isinstance(data, dict) else []
        if isinstance(addrs, list):
            self._anonymizers = {str(a).lower() for a in addrs if isinstance(a, str) and a.startswith("0x")}
        self._cex = {a.lower() for a in data.get("cex_hot_wallets", []) if isinstance(a, str) and a.startswith("0x")}
        self._dex = {a.lower() for a in data.get("dex_routers", []) if isinstance(a, str) and a.startswith("0x")}
        logger.info(
            "[ENTITY] blacklist loaded anon=%d cex=%d dex=%d",
            len(self._anonymizers), len(self._cex), len(self._dex),
        )

    def classify(self, address: str) -> tuple[str, str, float]:
        """Return (label, source, confidence)."""
        addr = address.lower()
        if addr in self._anonymizers:
            return ("ANONYMIZER", "blacklist", 1.0)
        if addr in self._cex:
            return ("CEX", "blacklist", 1.0)
        if addr in self._dex:
            return ("DEX_ROUTER", "blacklist", 1.0)
        return ("UNKNOWN", "default", 0.3)

    def persist_label(self, address: str, label: str, source: str, confidence: float) -> None:
        DBWriterQueue.put(
            """
            INSERT OR REPLACE INTO entity_labels
              (address, label, source, confidence, updated_ts_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (address.lower(), label, source, confidence, utc_now_rfc3339_ms()),
            table_hint="entity_labels",
        )


class TransferGraphBuilder:
    """Bounded BFS traversal of USDC.e transfer graph per wallet."""

    def __init__(self, api_key: str, linker: EntityLinker | None = None) -> None:
        self._api_key = api_key
        self._linker  = linker or EntityLinker()
        self._http: aiohttp.ClientSession | None = None

    async def _ensure_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=PER_HOP_TIMEOUT_SEC),
            )
        return self._http

    async def _fetch_inbound_transfers(
        self, wallet: str, from_block: int, to_block: int,
    ) -> list[dict]:
        """Paginated eth_getLogs in 9-block chunks, filtering to=wallet."""
        results: list[dict] = []
        url = ALCHEMY_HTTP_URL.format(api_key=self._api_key)
        session = await self._ensure_http()
        cur = from_block
        while cur <= to_block:
            end = min(cur + ETH_GETLOGS_PAGE_SIZE - 1, to_block)
            payload: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": hex(cur),
                    "toBlock":   hex(end),
                    "address":   USDC_E_ADDRESS,
                    "topics": [TRANSFER_TOPIC, None, _wallet_to_topic(wallet)],
                }],
            }
            try:
                async with session.post(url, json=payload) as resp:
                    data = await resp.json()
            except Exception as exc:
                logger.warning(
                    "[TG][FETCH_ERR] wallet=%s blocks=%d-%d exc=%s",
                    wallet[:10], cur, end, exc,
                )
                break
            logs = data.get("result") if isinstance(data, dict) else None
            if not isinstance(logs, list):
                break
            results.extend(logs)
            cur = end + 1
            await asyncio.sleep(0.2)
        return results

    def _decode_log(self, log: dict) -> dict | None:
        topics = log.get("topics") or []
        if len(topics) < 3:
            return None
        try:
            from_addr  = ("0x" + topics[1][-40:]).lower()
            to_addr    = ("0x" + topics[2][-40:]).lower()
            raw        = int(log.get("data", "0x0"), 16)
            usdc       = raw / (10 ** USDC_DECIMALS)
            block      = int(str(log.get("blockNumber", "0x0")), 16)
            tx_hash    = str(log.get("transactionHash", ""))
        except (ValueError, TypeError, KeyError):
            return None
        return {
            "from": from_addr,
            "to":   to_addr,
            "usdc_amount": usdc,
            "block":      block,
            "tx_hash":     tx_hash,
        }

    async def build_for_wallet(
        self, root_wallet: str, latest_block: int,
        max_hops: int = MAX_HOPS_DEFAULT,
    ) -> dict:
        edges: list[dict] = []
        visited: set[str] = {root_wallet.lower()}
        frontier: list[tuple[str, int]] = [(root_wallet.lower(), 1)]

        while frontier:
            wallet, hop = frontier.pop()
            if hop > max_hops:
                continue
            if len(visited) >= MAX_NODES_PER_GRAPH:
                logger.warning("[TG][NODE_CAP] wallet=%s visited=%d cap=%d",
                              root_wallet[:10], len(visited), MAX_NODES_PER_GRAPH)
                break
            from_block = max(0, latest_block - MAX_BLOCKS_PER_HOP)
            logs = await self._fetch_inbound_transfers(wallet, from_block, latest_block)
            for log in logs:
                t = self._decode_log(log)
                if not t:
                    continue
                edge = {
                    "root_wallet": root_wallet.lower(),
                    "from_addr":   t["from"],
                    "to_addr":     t["to"],
                    "usdc_amount": t["usdc_amount"],
                    "block":       t["block"],
                    "hop_depth":   hop,
                    "tx_hash":     t["tx_hash"],
                    "wallet_address": root_wallet.lower(),
                    "entity_link_score": 0.0,
                }
                label, source, conf = self._linker.classify(t["from"])
                edge["entity_link_score"] = max(0.0, min(1.0, float(conf)))
                edges.append(edge)
                self._linker.persist_label(t["from"], label, source, conf)
                if label == "EOA_PERSONAL" and t["from"] not in visited and hop < max_hops:
                    visited.add(t["from"])
                    frontier.append((t["from"], hop + 1))

        self._persist_edges(edges)
        return {"root": root_wallet.lower(), "edges": edges, "visited": list(visited)}

    def _persist_edges(self, edges: list[dict]) -> None:
        ts_utc = utc_now_rfc3339_ms()
        for e in edges:
            DBWriterQueue.put(
                """
                INSERT OR IGNORE INTO transfer_graph
                  (root_wallet, from_addr, to_addr, usdc_amount, block, hop_depth, tx_hash, created_ts_utc, wallet_address, entity_link_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (e["root_wallet"], e["from_addr"], e["to_addr"],
                 e["usdc_amount"], e["block"], e["hop_depth"],
                 e["tx_hash"], ts_utc, e["wallet_address"], e["entity_link_score"]),
                table_hint="transfer_graph",
            )

    async def close(self) -> None:
        if self._http and not self._http.closed:
            await self._http.close()


def fund_source_score_from_graph(graph: dict, linker: EntityLinker) -> float:
    """
    Aggregate fund_source_score (w4 component) from built graph dict.
    Returns 0..1: 1.0 = clean source, 0.0 = anonymizer present.
    """
    edges = graph.get("edges", [])
    if not edges:
        return 0.0
    weights: list[float] = []
    for e in edges:
        label, _, _ = linker.classify(e["from_addr"])
        if label == "ANONYMIZER":
            weights.append(0.0)
        elif label == "DEX_ROUTER":
            weights.append(0.5)
        elif label in ("CEX", "EOA_PERSONAL"):
            weights.append(1.0)
        else:
            weights.append(0.3)
    return sum(weights) / len(weights) if weights else 0.0