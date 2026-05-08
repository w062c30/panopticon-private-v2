"""
D180: Heavy market-level diagnostics (manual / on-demand only).

Aggregates hunting_shadow_hits, kyle_lambda_samples, polymarket_link_map,
and optional data/entropy_status.json for human-readable breakdown.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from panopticon_py.time_utils import utc_now_rfc3339_ms

SortKey = Literal["abs_z", "hits", "kyle_n", "recent"]


def _open_sqlite_ro(db_path: str) -> sqlite3.Connection:
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_market_breakdown(
    *,
    db_path: str | None = None,
    entropy_path: str | None = None,
    limit: int = 50,
    sort_key: SortKey = "abs_z",
) -> dict[str, Any]:
    """
    Returns aggregated rows for dashboard / CLI. Safe for large DBs: bounded LIMIT.

    sort_key:
      abs_z — max |entropy_z| descending
      hits — fire count descending
      kyle_n — Kyle sample count descending
      recent — last fire timestamp descending
    """
    t0 = time.perf_counter()
    db_path = db_path or os.getenv("PANOPTICON_DB_PATH", "data/panopticon.db")
    entropy_path = entropy_path or os.getenv("ENTROPY_STATUS_PATH", "data/entropy_status.json")
    lim = max(1, min(int(limit), 200))

    hits_by_market: dict[str, dict[str, Any]] = {}
    conn = _open_sqlite_ro(db_path)
    try:
        hit_rows = conn.execute(
            """
            SELECT market_id AS mid,
                   MAX(ABS(COALESCE(entropy_z, 0))) AS abs_z_max,
                   COUNT(*) AS fire_count,
                   AVG(COALESCE(sim_pnl_proxy, 0)) AS pnl_proxy_avg,
                   MAX(created_ts_utc) AS last_fire_ts
            FROM hunting_shadow_hits
            WHERE market_id IS NOT NULL AND TRIM(market_id) != ''
            GROUP BY market_id
            """
        ).fetchall()
        for r in hit_rows:
            mid = r["mid"]
            hits_by_market[mid] = {
                "abs_z_max": float(r["abs_z_max"] or 0.0),
                "fire_count": int(r["fire_count"] or 0),
                "pnl_proxy_avg": float(r["pnl_proxy_avg"] or 0.0),
                "last_fire_ts": r["last_fire_ts"],
            }

        kyle_rows = conn.execute(
            """
            SELECT asset_id AS aid, COUNT(*) AS n,
                   ROUND(AVG(lambda_obs), 8) AS avg_lambda
            FROM kyle_lambda_samples
            GROUP BY asset_id
            """
        ).fetchall()
        kyle_by_asset = {
            r["aid"]: {"kyle_n": int(r["n"]), "avg_lambda": float(r["avg_lambda"] or 0.0)}
            for r in kyle_rows
        }

        link_rows = conn.execute(
            """
            SELECT token_id, event_slug, market_slug
            FROM polymarket_link_map
            """
        ).fetchall()
        link_by_token = {
            r["token_id"]: {
                "question": r["event_slug"],
                "slug": r["market_slug"],
            }
            for r in link_rows
            if r["token_id"]
        }
    finally:
        conn.close()

    entropy_tokens: dict[str, Any] = {}
    try:
        raw = Path(entropy_path).read_text(encoding="utf-8")
        ej = json.loads(raw)
        entropy_tokens = ej.get("tokens") or {}
    except Exception:
        entropy_tokens = {}

    all_ids = set(hits_by_market) | set(kyle_by_asset) | set(entropy_tokens)

    rows: list[dict[str, Any]] = []
    for mid in all_ids:
        h = hits_by_market.get(mid, {})
        ky = kyle_by_asset.get(mid, {})
        link = link_by_token.get(mid, {})
        et = entropy_tokens.get(mid, {})
        question = link.get("question") or ""
        slug = link.get("slug") or ""
        rows.append(
            {
                "market_id": mid,
                "question": question or None,
                "slug": slug or None,
                "abs_z_max": float(h.get("abs_z_max") or 0.0),
                "fire_count": int(h.get("fire_count") or 0),
                "kyle_n": int(ky.get("kyle_n") or 0),
                "avg_lambda": float(ky.get("avg_lambda") or 0.0),
                "events": int(et.get("events") or 0) if et else 0,
                "h_hist": int(et.get("h_hist") or 0) if et else 0,
                "locked": bool(et.get("trigger_locked")) if et else False,
                "z_ready": bool(et.get("z_ready")) if et else False,
                "last_fire_ts": h.get("last_fire_ts"),
                "pnl_proxy_avg": float(h.get("pnl_proxy_avg") or 0.0),
            }
        )

    if sort_key == "recent":
        rows.sort(key=lambda r: r["last_fire_ts"] or "", reverse=True)
    elif sort_key == "hits":
        rows.sort(key=lambda r: (-r["fire_count"], r["market_id"]))
    elif sort_key == "kyle_n":
        rows.sort(key=lambda r: (-r["kyle_n"], r["market_id"]))
    else:
        rows.sort(key=lambda r: (-r["abs_z_max"], r["market_id"]))

    trimmed = rows[:lim]

    with_q = sum(1 for r in trimmed if r.get("question"))
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    return {
        "generated_at": utc_now_rfc3339_ms(),
        "elapsed_ms": elapsed_ms,
        "sort": sort_key,
        "limit": lim,
        "rows": trimmed,
        "summary": {
            "total_markets_in_slice": len(trimmed),
            "total_candidates_scanned": len(rows),
            "with_question": with_q,
            "without_question": len(trimmed) - with_q,
        },
    }
