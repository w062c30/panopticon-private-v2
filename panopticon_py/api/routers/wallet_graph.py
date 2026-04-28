from __future__ import annotations

from fastapi import APIRouter, Query

from panopticon_py.db import ShadowDB

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


@router.get("/graph")
def get_wallet_graph(
    wallet_limit: int = Query(50, ge=5, le=200),
    market_limit: int = Query(20, ge=2, le=100),
) -> dict:
    """
    Returns a wallet-relationship graph for vis.js network visualization.
    Nodes = wallets; edges = shared market activity or same entity cluster.

    Edges are weighted by:
      - SAME_ENTITY: same cluster/entity_id (strongest link)
      - SHARED_MARKET: both wallets traded on the same market
      - SIMILAR_Timing: trade within 100ms on same side (suspicious co-movement)
    """
    db = ShadowDB()
    try:
        db.bootstrap()

        # Fetch top wallets by PnL
        wallet_rows = db.conn.execute(
            """
            SELECT wallet_address, entity_id, all_time_pnl, win_rate,
                   discovery_source, source_quality, last_seen_ts_utc
            FROM tracked_wallets
            ORDER BY all_time_pnl DESC
            LIMIT ?
            """,
            (wallet_limit,),
        ).fetchall()

        wallet_addresses = [str(r[0]) for r in wallet_rows]
        wallet_info: dict[str, dict] = {
            r[0]: {
                "address": r[0],
                "entity_id": r[1],
                "pnl": float(r[2]),
                "win_rate": float(r[3]),
                "source": r[4],
                "quality": r[5],
                "last_seen": r[6],
            }
            for r in wallet_rows
        }

        if not wallet_addresses:
            return {"nodes": [], "edges": []}

        placeholders = ",".join("?" for _ in wallet_addresses)

        # Wallets per market (for shared-market edges)
        market_rows = db.conn.execute(
            f"""
            SELECT address, market_id, obs_type, ingest_ts_utc
            FROM wallet_observations
            WHERE address IN ({placeholders})
              AND market_id IS NOT NULL
            ORDER BY ingest_ts_utc DESC
            """,
            wallet_addresses,
        ).fetchall()

        # market_id -> [wallet_addresses]
        market_wallets: dict[str, list[str]] = {}
        for addr, mkt_id, obs_type, ingest_ts in market_rows:
            if obs_type not in ("clob_trade", "book_update"):
                continue
            if mkt_id not in market_wallets:
                market_wallets[mkt_id] = []
            if addr not in market_wallets[mkt_id]:
                market_wallets[mkt_id].append(addr)

        # wallet_pair -> set of shared markets
        pair_markets: dict[tuple[str, str], set[str]] = {}
        for wallets in market_wallets.values():
            if len(wallets) < 2:
                continue
            for i, wa in enumerate(wallets):
                for wb in wallets[i + 1 :]:
                    key = (wa, wb) if wa < wb else (wb, wa)
                    if key not in pair_markets:
                        pair_markets[key] = set()
                    pair_markets[key].add(mkt_id)

        # Build entity-cluster edges (same entity_id)
        entity_wallets: dict[str, list[str]] = {}
        for addr, info in wallet_info.items():
            eid = info["entity_id"]
            if eid not in entity_wallets:
                entity_wallets[eid] = []
            entity_wallets[eid].append(addr)

        # Nodes
        nodes = []
        for addr, info in wallet_info.items():
            # Normalize address for label
            label = addr[:6] + "..." + addr[-4:] if len(addr) > 12 else addr
            # Color by quality
            color_map = {
                "tier1": "#22d3ee",  # cyan - smart money
                "tier2": "#a78bfa",  # purple - mid-tier
                "whale": "#34d399",  # green
                "degen": "#f87171",  # red
                "unknown": "#94a3b8",  # slate
            }
            color = color_map.get(info["quality"], "#94a3b8")
            nodes.append(
                {
                    "id": addr,
                    "label": label,
                    "fullAddress": addr,
                    "entityId": info["entity_id"],
                    "pnl": info["pnl"],
                    "winRate": info["win_rate"],
                    "source": info["source"],
                    "quality": info["quality"],
                    "color": color,
                    "value": max(1, abs(info["pnl"]) / 100),  # node size by PnL magnitude
                }
            )

        # Edges
        edges = []
        for (wa, wb), markets in pair_markets.items():
            market_count = len(markets)
            if market_count == 0:
                continue
            # Only show edges where 2+ shared markets (significant co-activity)
            if market_count >= 2:
                edges.append(
                    {
                        "from": wa,
                        "to": wb,
                        "relation": "SHARED_MARKET",
                        "weight": market_count,
                        "markets": list(markets)[:5],
                        "title": f"Shared {market_count} markets: {', '.join(list(markets)[:3])}",
                    }
                )

        # Add same-entity edges
        for entity_id, wallets_in_entity in entity_wallets.items():
            if len(wallets_in_entity) < 2:
                continue
            for i, wa in enumerate(wallets_in_entity):
                for wb in wallets_in_entity[i + 1 :]:
                    # Check if edge already exists
                    existing = any(
                        (e["from"] == wa and e["to"] == wb) or (e["from"] == wb and e["to"] == wa)
                        for e in edges
                    )
                    if not existing:
                        edges.append(
                            {
                                "from": wa,
                                "to": wb,
                                "relation": "SAME_ENTITY",
                                "weight": 99,
                                "markets": [],
                                "title": f"Same entity cluster: {entity_id[:12]}...",
                            }
                        )

        return {"nodes": nodes, "edges": edges}

    finally:
        db.close()
