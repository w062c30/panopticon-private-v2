#!/usr/bin/env python3
"""
Market_Semantic_Router: background worker (Gamma poll -> NVIDIA semantics -> cluster_mapping.json).

Does not touch Track X / L1 WS. Phase 1: NVIDIA only (see AGENTS.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from typing import Any
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request

# Allow running as script from repo root
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from panopticon_py.hunting.semantic_router import (
    gamma_market_id,
    gamma_title_description_tags,
    merge_market_cluster_row,
    nvidia_extract_market_semantics,
    read_cluster_mapping_full,
    write_cluster_mapping_atomic,
)
from panopticon_py.load_env import load_repo_env

logger = logging.getLogger("semantic_router_daemon")


def _fetch_gamma_markets(
    base: str,
    *,
    limit: int,
    offset: int,
    timeout_sec: float,
) -> list[dict[str, Any]]:
    q = urlparse.urlencode({"limit": str(limit), "offset": str(offset), "closed": "false"})
    url = f"{base.rstrip('/')}/markets?{q}"
    user_agent = os.getenv(
        "SEMANTIC_ROUTER_USER_AGENT",
        "Mozilla/5.0 (compatible; PanopticonSemanticRouter/1.0; +https://gamma-api.polymarket.com)",
    )
    req = request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": user_agent,
        },
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return [x for x in data["markets"] if isinstance(x, dict)]
    return []


def _process_once(
    *,
    gamma_base: str,
    mapping_path: str,
    limit: int,
    dry_run: bool,
    max_pages: int,
) -> int:
    mapping = read_cluster_mapping_full(mapping_path)
    processed = set(mapping.keys())
    new_count = 0
    markets: list[dict[str, Any]] = []
    for page in range(max(1, max_pages)):
        offset = page * limit
        try:
            chunk = _fetch_gamma_markets(gamma_base, limit=limit, offset=offset, timeout_sec=60.0)
        except (urlerror.URLError, urlerror.HTTPError, TimeoutError, json.JSONDecodeError, OSError) as e:
            logger.error("Gamma fetch failed offset=%s: %s", offset, e)
            break
        if not chunk:
            break
        markets.extend(chunk)

    for obj in markets:
        mid = gamma_market_id(obj)
        if not mid or mid in processed:
            continue
        title, desc, tags = gamma_title_description_tags(obj)
        if not title and not desc:
            logger.warning("skip market id=%s (no title/description)", mid)
            semantics = nvidia_extract_market_semantics("(empty)", "", [], api_key=os.getenv("NVIDIA_API_KEY"))
        else:
            semantics = nvidia_extract_market_semantics(title, desc, tags, api_key=os.getenv("NVIDIA_API_KEY"))
        extra = {"source": "gamma", "title": title[:500]}
        mapping = merge_market_cluster_row(mapping, mid, semantics, extra=extra)
        processed.add(mid)
        new_count += 1
        logger.info(
            "mapped market_id=%s cluster_id=%s direction=%s",
            mid,
            semantics.get("Parent_Theme"),
            semantics.get("Directional_Vector"),
        )

    if new_count and not dry_run:
        write_cluster_mapping_atomic(mapping_path, mapping)
    elif new_count and dry_run:
        logger.info("dry-run: would write %s new entries", new_count)
    return new_count


def main() -> int:
    load_repo_env()
    logging.basicConfig(
        level=os.getenv("SEMANTIC_ROUTER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Market semantic router (Gamma + NVIDIA -> cluster_mapping.json)")
    ap.add_argument("--once", action="store_true", help="Single poll cycle then exit")
    ap.add_argument("--interval-sec", type=float, default=float(os.getenv("SEMANTIC_ROUTER_INTERVAL_SEC", "300")))
    ap.add_argument("--gamma-base", default=os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com"))
    ap.add_argument(
        "--mapping-path",
        default=os.getenv("CLUSTER_MAPPING_PATH", os.path.join("data", "cluster_mapping.json")),
    )
    ap.add_argument("--limit", type=int, default=int(os.getenv("GAMMA_MARKETS_LIMIT", "100")))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logger.info(
        "Market_Semantic_Router start gamma_base=%s mapping=%s interval=%s",
        args.gamma_base,
        args.mapping_path,
        args.interval_sec,
    )

    max_pages = int(os.getenv("GAMMA_MARKETS_MAX_PAGES", "5"))
    if args.once:
        n = _process_once(
            gamma_base=args.gamma_base,
            mapping_path=args.mapping_path,
            limit=args.limit,
            dry_run=args.dry_run,
            max_pages=max_pages,
        )
        logger.info("once: processed %s new markets", n)
        return 0

    try:
        while True:
            try:
                n = _process_once(
                    gamma_base=args.gamma_base,
                    mapping_path=args.mapping_path,
                    limit=args.limit,
                    dry_run=args.dry_run,
                    max_pages=max_pages,
                )
                if n:
                    logger.info("cycle: wrote %s new markets", n)
            except Exception:
                logger.exception("cycle failed")
            time.sleep(max(30.0, float(args.interval_sec)))
    except KeyboardInterrupt:
        logger.info("shutdown requested")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
