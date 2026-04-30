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
import json
import logging
import urllib.parse

from panopticon_py.db import ShadowDB
from panopticon_py.time_utils import utc_now_rfc3339_ms

logger = logging.getLogger(__name__)

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
    db: ShadowDB,
    upserted_ids: set[str],
) -> bool:
    """
    D111: Process single Gamma API market dict.
    Side effects: calls db.upsert_pol_market(), modifies upserted_ids.
    Returns True if upserted, False if filtered.
    """
    slug = (m.get("slug") or "").lower()
    if any(seg in slug for seg in _EXCLUDE_SLUG_SEGMENTS):
        return False
    try:
        vol      = float(m.get("volume")  or 0)
        best_bid = float(m.get("bestBid") or 0.5)
    except (ValueError, TypeError):
        return False
    if vol < 5000 or best_bid >= 0.99 or best_bid <= 0.01:
        return False
    matched_kw = [kw for kw in POL_KEYWORDS if kw in slug]
    if not matched_kw:
        return False
    market_id = m.get("conditionId") or m.get("id") or ""
    if not market_id:
        return False
    category = next(
        (POL_CATEGORY_MAP[kw] for kw in matched_kw if kw in POL_CATEGORY_MAP),
        "OTHER",
    )
    token_id_yes, token_id_no = _extract_token_ids(m)   # D111: unpack tuple
    db.upsert_pol_market({
        "market_id":          market_id,
        "token_id":           token_id_yes,
        "token_id_no":        token_id_no,              # D111: NO-side token
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
    offset = 0
    limit = 100
    upserted_ids: set[str] = set()

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

            for m in markets:
                if _process_market_record(m, db, upserted_ids):
                    count += 1

            if len(markets) < limit:
                break
            offset += limit
            await asyncio.sleep(0.5)  # rate limit guard — outside semaphore

    # D102-2: Deactivate markets not seen in this scan
    if upserted_ids:
        db.deactivate_closed_pol_markets(upserted_ids)

    # D103: Zero-result diagnostic
    if count == 0 and upserted_ids:
        logger.warning(
            "[POL_SCAN] API returned markets but NONE matched POL_KEYWORDS filter. "
            "Review POL_KEYWORDS list or filter criteria (vol>=5000, bestBid range). "
            "Current keywords: %s",
            POL_KEYWORDS,
        )
    elif count == 0 and not upserted_ids:
        logger.warning(
            "[POL_SCAN] Zero markets upserted — API may be unreachable or returned empty. "
            "Existing watchlist preserved (deactivation skipped)."
        )

    logger.info("[POL_SCAN] upserted=%d political markets", count)
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
    offset = 0
    limit = 100
    upserted_ids: set[str] = set()

    for _ in range(max_pages):
        try:
            base = "https://gamma-api.polymarket.com"
            path = "/markets"
            url = f"{base}{path}?active=true&closed=false&archived=false&limit={limit}&offset={offset}"
            resp = httpx.get(url, timeout=10.0)
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

        for m in markets:
            if _process_market_record(m, db, upserted_ids):
                count += 1

        if len(markets) < limit:
            break
        offset += limit

    # D102-2: Deactivate markets not seen in this scan
    if upserted_ids:
        db.deactivate_closed_pol_markets(upserted_ids)

    # D103: Zero-result diagnostic
    if count == 0 and upserted_ids:
        logger.warning(
            "[POL_SCAN][SYNC] API returned markets but NONE matched POL_KEYWORDS filter. "
            "Review POL_KEYWORDS list or filter criteria (vol>=5000, bestBid range). "
            "Current keywords: %s",
            POL_KEYWORDS,
        )
    elif count == 0 and not upserted_ids:
        logger.warning(
            "[POL_SCAN][SYNC] Zero markets upserted — API may be unreachable or returned empty. "
            "Existing watchlist preserved (deactivation skipped)."
        )

    logger.info("[POL_SCAN][SYNC] upserted=%d political markets", count)
    return count