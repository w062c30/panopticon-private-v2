"""Hidden Link Graph Engine: HFT Firm Cluster Detection.

After an UNDERLYING_SHOCK is detected, this module ingests the real Polymarket
CLOB takers within a 500ms post-shock window and builds a networkx graph.
Wallets are linked via two edge types:
  1. Temporal Sync Edge (weight 0.8) — multiple wallets attack the same stale
     quote within < 100ms.
  2. Funding Overlap Edge (weight 1.0) — wallets share a common funding root.

Community detection (connected components + edge-weight threshold) collapses
these into HFT_FIRM_CLUSTER virtual entities, persisted to the DB.

Aligns with:
  [Invariant 2.2] Graph Entity Folding
  [Invariant 2.4] Cache-First DAL — DB lookup before Moralis API calls
  [Invariant 2.3] Mandatory 4D Scrubbing — cluster members inherit parent filters
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import networkx as nx

from panopticon_py.db import ShadowDB

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Edge-weight constants
# --------------------------------------------------------------------------- #

WEIGHT_TEMPORAL_SYNC: float = 0.8   # wallets sync within 100ms on same quote
WEIGHT_FUNDING_OVERLAP: float = 1.0  # wallets share a funding root
WEIGHT_COMPOSITE_THRESHOLD: float = 0.5  # minimum avg edge weight for cluster
SOFT_CLUSTER_MIN_SIZE: int = 3      # min wallets for soft clustering
SOFT_CLUSTER_THRESHOLD: float = 0.3  # avg weight threshold for soft cluster


# --------------------------------------------------------------------------- #
#  Funding-root cache (Cache-First per Invariant 2.4)
# --------------------------------------------------------------------------- #

class FundingRootCache:
    """
    SQLite-backed LRU cache for wallet funding roots.
    Checked BEFORE any external API call (Moralis).

    Thread-safe: uses a lock around all DB operations.  Connections are
    opened per-call with ``check_same_thread=False`` to avoid conflicts
    when accessed from the async HFT event loop running on a different
    thread than the ``FrictionStateWorker``.
    """

    def __init__(self, db_path: str = "data/panopticon.db") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the funding_roots_cache table if absent."""
        try:
            with sqlite3.connect(self._db_path, check_same_thread=False) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS funding_roots_cache (
                        wallet_address TEXT PRIMARY KEY,
                        roots_json     TEXT NOT NULL,
                        cached_at_utc  TEXT NOT NULL
                    )
                """)
        except sqlite3.Error:
            logger.exception("funding_root_cache_init_error")

    def get(self, wallet: str) -> list[str] | None:
        """Return funding roots list if cached, else None."""
        w = wallet.lower()[:42]
        with self._lock:
            try:
                with sqlite3.connect(self._db_path, check_same_thread=False) as conn:
                    row = conn.execute(
                        "SELECT roots_json FROM funding_roots_cache WHERE wallet_address = ?",
                        (w,),
                    ).fetchone()
                if row:
                    return json.loads(row[0])
                return None
            except sqlite3.Error:
                logger.warning("funding_root_cache_read_error", extra={"wallet": w})
                return None

    def put(self, wallet: str, roots: list[str]) -> None:
        """Cache funding roots for a wallet."""
        w = wallet.lower()[:42]
        with self._lock:
            try:
                with sqlite3.connect(self._db_path, check_same_thread=False) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO funding_roots_cache
                            (wallet_address, roots_json, cached_at_utc)
                        VALUES (?, ?, ?)
                        """,
                        (w, json.dumps(roots[:20]), datetime.now(timezone.utc).isoformat()),
                    )
            except sqlite3.Error:
                logger.warning("funding_root_cache_write_error", extra={"wallet": w})


# --------------------------------------------------------------------------- #
#  Cluster candidate from a post-shock taker snapshot
# --------------------------------------------------------------------------- #

@dataclass
class TakerTrade:
    """One real trade taker on Polymarket CLOB after a shock."""
    address:      str
    ts_ms:        float   # exchange-provided or recv wall-clock ms
    notional_usd: float
    side:         str     # "BUY" | "SELL"
    market_id:    str     # token / condition ID


# --------------------------------------------------------------------------- #
#  HiddenLinkGraphEngine
# --------------------------------------------------------------------------- #

class HiddenLinkGraphEngine:
    """
    Builds and analyses a wallet-correlation graph after each UNDERLYING_SHOCK.

    Usage::

        engine = HiddenLinkGraphEngine(db=ShadowDB())
        engine.ingest_shock_takers(takers=[...], shock_ts_ms=..., shock_id="abc")
        clusters = engine.compute_hft_clusters()
        engine.persist_clusters(clusters, shock_id="abc")
    """

    def __init__(self, db: ShadowDB | None = None) -> None:
        self._db     = db or ShadowDB()
        self._cache  = FundingRootCache()
        self._G:     nx.Graph = nx.Graph()
        self._shock_takers: deque[list[TakerTrade]] = deque(maxlen=50)  # recent windows

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    def ingest_shock_takers(
        self,
        takers: list[dict[str, Any]],
        shock_ts_ms: float,
        shock_id: str,
        *,
        temporal_window_ms: float = 100.0,
        funding_window_ms:  float = 500.0,
    ) -> int:
        """
        Ingest a list of Polymarket CLOB takers that fired within
        ``funding_window_ms`` of the shock.

        Returns the number of unique wallet nodes added to the graph.

        Edge-building rules:
          1. Temporal Sync (weight 0.8): two wallets trade within
             ``temporal_window_ms`` of each other on the same side.
          2. Funding Overlap (weight 1.0): wallets share a cached funding root.
        """
        # Parse into TakerTrade objects
        parsed: list[TakerTrade] = []
        for t in takers:
            try:
                parsed.append(TakerTrade(
                    address     = str(t["address"] or t.get("proxyWallet") or "").lower()[:42],
                    ts_ms       = float(t.get("ts_ms") or t.get("timestamp") or 0),
                    notional_usd= abs(float(t.get("notional_usd") or t.get("size") or 0)),
                    side        = str(t.get("side") or "BUY").upper(),
                    market_id   = str(t.get("market_id") or t.get("token_id") or ""),
                ))
            except (TypeError, ValueError):
                continue

        if not parsed:
            return 0

        self._shock_takers.append(parsed)
        _add_nodes(self._G, parsed)

        # Edge Type 1: Temporal Sync
        temporal_edges = _build_temporal_edges(parsed, temporal_window_ms)
        for (a, b, weight) in temporal_edges:
            _add_weighted_edge(self._G, a, b, weight, attr="temporal_sync")

        # Edge Type 2: Funding Overlap (Cache-First)
        funding_edges = _build_funding_edges(
            parsed,
            self._cache,
            self._db,
            funding_window_ms,
        )
        for (a, b, weight) in funding_edges:
            _add_weighted_edge(self._G, a, b, weight, attr="funding_overlap")

        n_added = len(parsed)
        logger.info(
            "HFT_GRAPH_INGEST",
            extra={
                "shock_id":          shock_id,
                "takers_ingested":   n_added,
                "total_nodes":       self._G.number_of_nodes(),
                "total_edges":       self._G.number_of_edges(),
                "temporal_edges":    len(temporal_edges),
                "funding_edges":     len(funding_edges),
            },
        )
        return n_added

    def compute_hft_clusters(
        self,
        *,
        strong_threshold: float = WEIGHT_COMPOSITE_THRESHOLD,
        soft_threshold:    float = SOFT_CLUSTER_THRESHOLD,
        soft_min_size:     int   = SOFT_CLUSTER_MIN_SIZE,
    ) -> list[frozenset[str]]:
        """
        Louvain-style community detection via connected components +
        edge-weight thresholding.

        Returns a list of wallet frozensets, each representing one
        HFT_FIRM_CLUSTER.
        """
        if self._G.number_of_nodes() < 2:
            return []

        components = list(nx.connected_components(self._G))
        clusters: list[frozenset[str]] = []

        for comp in components:
            if len(comp) < 2:
                continue
            subgraph = self._G.subgraph(comp)
            avg_w = _avg_edge_weight(subgraph)
            max_w = _max_edge_weight(subgraph)

            # Strong cluster: avg edge weight above threshold
            if avg_w >= strong_threshold:
                clusters.append(frozenset(comp))
            # Soft cluster: large enough and moderate weight
            elif len(comp) >= soft_min_size and avg_w >= soft_threshold:
                clusters.append(frozenset(comp))

        if clusters:
            logger.info(
                "HFT_CLUSTERS_DETECTED",
                extra={
                    "clusters":       len(clusters),
                    "total_clustered": sum(len(c) for c in clusters),
                    "avg_weights":    [round(_avg_edge_weight(self._G.subgraph(c)), 3) for c in clusters],
                },
            )
        return clusters

    def persist_clusters(
        self,
        clusters: list[frozenset[str]],
        shock_id: str,
    ) -> None:
        """
        Persist detected HFT_FIRM_CLUSTERs to the DB:
          - ``discovered_entities`` (upsert)
          - ``tracked_wallets``    (upsert)
          - ``virtual_entity_events`` (append)
        """
        ts_now = datetime.now(timezone.utc).isoformat()

        for idx, cluster in enumerate(clusters):
            entity_id = _derive_entity_id(cluster)
            wallets   = sorted(cluster)
            avg_w     = _avg_edge_weight(self._G.subgraph(cluster)) if cluster else 0.0
            trust     = min(95.0, 30.0 + avg_w * 65.0)  # scale avg_w [0→30] to [30→95]

            self._db.upsert_discovered_entity({
                "entity_id":   entity_id,
                "trust_score": trust,
                "primary_tag": "ALGO_SLICING",
                "sample_size": len(wallets),
                "last_updated_at": ts_now,
            })

            for wallet in wallets:
                self._db.upsert_tracked_wallet({
                    "wallet_address":    wallet,
                    "entity_id":         entity_id,
                    "all_time_pnl":      0.0,
                    "win_rate":          0.0,
                    "discovery_source":   "HFT_SHOCK_DETECTION",
                    "source_quality":    "graph_inferred",
                    "history_sample_size": 0,
                    "last_seen_ts_utc":  ts_now,
                    "last_updated_at":   ts_now,
                })

            self._db.append_virtual_entity_event({
                "event_id":        f"hft_shock_{shock_id}_{entity_id}_{idx}",
                "entity_id":       entity_id,
                "members_json":     json.dumps(wallets),
                "classification":  "HFT_FIRM_CLUSTER",
                "payload_json": json.dumps({
                    "shock_id":       shock_id,
                    "cluster_idx":    idx,
                    "avg_edge_weight": round(avg_w, 4),
                    "wallet_count":   len(wallets),
                }),
                "created_ts_utc": ts_now,
            })

            logger.info(
                "HFT_CLUSTER_PERSISTED",
                extra={
                    "entity_id":      entity_id,
                    "wallets":        len(wallets),
                    "avg_edge_weight": round(avg_w, 3),
                    "trust_score":    round(trust, 2),
                    "shock_id":       shock_id,
                },
            )

    def clear(self) -> None:
        """Reset the graph between shock cycles."""
        self._G.clear()

    @property
    def graph(self) -> nx.Graph:
        return self._G

    # ------------------------------------------------------------------ #
    #  Internal edge builders
    # ------------------------------------------------------------------ #

    @staticmethod
    def _roots_overlap(a: list[str], b: list[str]) -> bool:
        return bool(set(a).intersection(set(b)))

    @staticmethod
    def _derive_entity_id(wallets: frozenset[str]) -> str:
        normalized = sorted({w.lower()[:42] for w in wallets if w.startswith("0x")})
        basis = "|".join(normalized) if normalized else "empty"
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
        return f"ve_hft_{digest}"


# --------------------------------------------------------------------------- #
#  Standalone helpers (top-level for testability)
# --------------------------------------------------------------------------- #

def _add_nodes(G: nx.Graph, takers: list[TakerTrade]) -> None:
    for t in takers:
        addr = t.address
        if not addr.startswith("0x"):
            continue
        if addr not in G:
            G.add_node(addr, first_seen_ms=t.ts_ms, side=t.side)
        else:
            # Update side if already present
            G.nodes[addr]["side"] = t.side


def _build_temporal_edges(
    takers: list[TakerTrade],
    window_ms: float,
) -> list[tuple[str, str, float]]:
    """
    Pair wallets that traded within ``window_ms`` of each other.
    Weight = (1 - dt/window_ms) * WEIGHT_TEMPORAL_SYNC.
    """
    edges: list[tuple[str, str, float]] = []
    n = len(takers)
    for i in range(n):
        wi = takers[i]
        if not wi.address.startswith("0x"):
            continue
        for j in range(i + 1, n):
            wj = takers[j]
            if not wj.address.startswith("0x"):
                continue
            dt = abs(wi.ts_ms - wj.ts_ms)
            if dt <= window_ms and wi.side == wj.side:
                weight = (1.0 - dt / window_ms) * WEIGHT_TEMPORAL_SYNC
                edges.append((wi.address, wj.address, weight))
    return edges


def _build_funding_edges(
    takers: list[TakerTrade],
    cache:  FundingRootCache,
    db:     ShadowDB,
    window_ms: float,
) -> list[tuple[str, str, float]]:
    """
    For each taker, try to load funding roots from cache (Invariant 2.4).
    If not cached, synchronously load from DB ``wallet_observations`` first,
    then Moralis as last resort.  Edge if roots overlap.
    """
    roots_by_wallet: dict[str, list[str]] = {}
    edges: list[tuple[str, str, float]] = []

    for taker in takers:
        addr = taker.address
        if not addr.startswith("0x"):
            continue

        # [Invariant 2.4] Cache-First: check local cache
        cached = cache.get(addr)
        if cached is not None:
            roots_by_wallet[addr] = cached
            continue

        # Fallback: check local DB wallet_observations for funding context
        db_roots = _load_roots_from_db(db, addr)
        if db_roots:
            roots_by_wallet[addr] = db_roots
            cache.put(addr, db_roots)
            continue

        # Last resort: trace via Moralis (expensive, async-safe — skip here for sync path)
        try:
            from panopticon_py.hunting.moralis_client import fetch_wallet_erc20_transfers_capped
            rows = fetch_wallet_erc20_transfers_capped(addr, row_hard_cap=50)
            roots = list({
                str(r.get("from_address") or r.get("from") or "").lower()[:42]
                for r in rows
                if str(r.get("from_address") or r.get("from") or "").startswith("0x")
            })
            if roots:
                roots_by_wallet[addr] = roots
                cache.put(addr, roots)
        except Exception:
            roots_by_wallet[addr] = []

    # Build edges wherever funding roots overlap
    addrs = list(roots_by_wallet.keys())
    for i, ai in enumerate(addrs):
        for aj in addrs[i + 1:]:
            if HiddenLinkGraphEngine._roots_overlap(
                roots_by_wallet[ai], roots_by_wallet[aj]
            ):
                edges.append((ai, aj, WEIGHT_FUNDING_OVERLAP))

    return edges


def _load_roots_from_db(db: ShadowDB, wallet: str) -> list[str]:
    """
    Retrieve persisted funding roots from the ``wallet_funding_roots`` ShadowDB table
    (populated by the discovery loop's ``trace_funding_roots()`` call).

    Falls back to scanning ``wallet_observations`` payload_json for legacy data.

    Returns a list of root addresses found, or empty list.
    """
    try:
        # Primary path: wallet_funding_roots table (populated by discovery loop)
        roots = db.fetch_wallet_funding_roots(wallet.lower()[:42])
        if roots:
            return roots

        # Legacy fallback: wallet_observations payload_json (handles pre-existing data)
        obs_rows = db.fetch_recent_wallet_observations(wallet.lower(), limit=200)
        legacy_roots: set[str] = set()
        for row in obs_rows:
            payload = row.get("payload") or {}
            if isinstance(payload, dict):
                fr = payload.get("funding_root") or payload.get("from_address") or ""
                if isinstance(fr, str) and fr.startswith("0x"):
                    legacy_roots.add(fr.lower()[:42])
        return sorted(legacy_roots)
    except Exception:
        return []


def _add_weighted_edge(
    G: nx.Graph,
    a: str,
    b: str,
    weight: float,
    *,
    attr: str = "weight",
) -> None:
    """
    Add or update an edge.  If edge already exists, keep the MAX weight
    (strongest signal wins).
    """
    if G.has_edge(a, b):
        existing = G.edges[a, b].get(attr, 0.0)
        if weight > existing:
            G.edges[a, b][attr] = weight
    else:
        G.add_edge(a, b, **{attr: weight, "weight": weight})


def _avg_edge_weight(subgraph: nx.Graph) -> float:
    edges = list(subgraph.edges(data=True))
    if not edges:
        return 0.0
    return sum(e[2].get("weight", 0.0) for e in edges) / len(edges)


def _max_edge_weight(subgraph: nx.Graph) -> float:
    edges = list(subgraph.edges(data=True))
    if not edges:
        return 0.0
    return max((e[2].get("weight", 0.0) for e in edges), default=0.0)


def _derive_entity_id(wallets: frozenset[str]) -> str:
    normalized = sorted({w.lower()[:42] for w in wallets if w.startswith("0x")})
    basis = "|".join(normalized) if normalized else "empty"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"ve_hft_{digest}"
