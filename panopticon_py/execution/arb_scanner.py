"""
D118-1: Arb Scanner — Auto Market ID Injection
目標：自動從 Gamma API 發現 T5 Sports 市場，WebSocket 監聽訂單簿
本階段：只記錄，不下單（paper mode）

D117 skeleton → D118: auto-discover T5 market IDs, auto-refresh every 60s.
"""
from __future__ import annotations

import asyncio
import httpx
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROCESS_VERSION = "v0.2.0-D118"

ARB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ARB_THRESHOLD = 0.97
ARB_MIN_DEPTH = 50
PAPER_MODE = True  # enforced — no real orders


# ---------------------------------------------------------------------------
# T5 Sports Filter (mirrors run_radar.py logic; duplicated here to keep arb_scanner self-contained)
# ---------------------------------------------------------------------------
_TIER5_SPORTS_CATEGORIES = [
    "sports",
    "basketball",
    "football",
    "soccer",
    "tennis",
    "baseball",
    "hockey",
    "ufc",
    "boxing",
    "mma",
    "golf",
    "nascar",
    "f1",
    "esports",
    "olympics",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "epl",
    "la_liga",
    "champions_league",
    "serie_a",
    "bundesliga",
    "ligue_1",
    "cricket",
    "rugby",
]
_TIER5_SLUG_SPORTS_KEYWORDS = [
    "nba-", "nfl-", "mlb-", "nhl-", "epl-", "la-liga-",
    "champions-league-", "serie-a-", "bundesliga-", "ligue-1-",
    "-vs-", "-to-win-", "total-goals-", "player-props-",
    "nba-finals", "nba-eastern", "nba-western",
    "nfl-playoffs", "super-bowl",
    "world-cup", "euro-cup",
    "ufc-", "mma-", "boxing-",
    "-win-the-",
]
_TIER5_EXCLUDE_SEASON_KEYWORDS = [
    "season-winner", "championship-winner", "league-winner",
    "most-valuable-player", "mvp-", "regular-season",
]
_TIER5_SLUG_POL_GUARD = [
    "trump", "election", "tariff", "senate", "btc", "eth", "crypto",
    "will-trump", "president", "government",
    # D118: Preventive expansion
    "harris", "biden", "desantis", "pelosi", "zuckerberg", "musk",
]
_TIER5_MAX_END_SEC = 3888000   # 45 days; must match run_radar.py constant


def _is_tier5_sports_market(m: dict) -> bool:
    """Mirror of run_radar._is_tier5_sports_market — kept in sync."""
    slug = str(m.get("slug") or "").lower()
    category_lc = str(m.get("groupItemTitle") or m.get("category") or "").lower()
    category_match = any(s in category_lc for s in _TIER5_SPORTS_CATEGORIES)
    slug_match = any(kw in slug for kw in _TIER5_SLUG_SPORTS_KEYWORDS)

    if not (category_match or slug_match):
        return False

    if slug_match and not category_match:
        if any(g in slug for g in _TIER5_SLUG_POL_GUARD):
            return False

    if not m.get("active"):
        return False

    end_iso = m.get("endDateIso") or ""
    if end_iso:
        try:
            end_dt_raw = end_iso.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(end_dt_raw)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            else:
                offset = end_dt.utcoffset()
                if offset is not None:
                    end_dt = (end_dt.replace(tzinfo=None) - offset).replace(tzinfo=timezone.utc)
            delta_sec = (end_dt - datetime.now(timezone.utc)).total_seconds()
            if delta_sec > _TIER5_MAX_END_SEC:
                return False
        except (ValueError, TypeError):
            return False

    return True


async def fetch_t5_market_ids() -> list[str]:
    """
    D118-1: Fetch active T5 sports market_ids from Gamma API.
    Returns list of Polymarket market_id strings that pass _is_tier5_sports_market.
    """
    market_ids: list[str] = []
    seen: set[str] = set()
    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        url = f"{base}{path}?closed=false&limit=500"
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        logger.warning("[ARB] Failed to fetch T5 markets from Gamma: %s", e)
        return []

    if not isinstance(markets, list):
        return []

    for m in markets:
        if not isinstance(m, dict):
            continue
        if _is_tier5_sports_market(m):
            mid = str(m.get("id") or m.get("market_id") or "")
            if mid and mid not in seen:
                seen.add(mid)
                market_ids.append(mid)

    return market_ids


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class ArbOpportunity:
    ts: float
    market_id: str
    yes_price: float
    no_price: float
    total: float
    depth: float
    locked_profit_per_100: float
    paper_mode: bool = PAPER_MODE


@dataclass
class ArbScanner:
    paper_mode: bool = PAPER_MODE
    books: dict[str, dict[str, PriceLevel]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    opportunities_log: list[ArbOpportunity] = field(default_factory=list)
    _ws: object = field(default=None, init=False, repr=False)
    _refresh_interval: int = field(default=60, init=False)

    async def run(self) -> None:
        """
        D118-1: Main entry point.
        Auto-discovers T5 market IDs from Gamma API, refreshes every 60s.
        """
        from panopticon_py.utils.process_guard import acquire_singleton
        acquire_singleton("arb_scanner", PROCESS_VERSION)

        logger.info("[ARB] Starting ArbScanner paper_mode=%s auto_refresh=%ds",
                    PAPER_MODE, self._refresh_interval)
        current_mids: list[str] = []

        while True:
            try:
                # ── Step 1: Auto-discover T5 market IDs ──────────────────────────
                new_mids = await fetch_t5_market_ids()
                if new_mids:
                    current_mids = new_mids
                    logger.info("[ARB] Discovered %d T5 markets via Gamma API", len(current_mids))
                elif not current_mids:
                    logger.warning("[ARB] No T5 markets found; retrying in 30s…")
                    await asyncio.sleep(30)
                    continue

                # ── Step 2: Run WS listener with current_mids ─────────────────────
                await self._connect_and_listen(current_mids)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ARB] Error in run loop: %s; reconnecting in 10s…", e)
                await asyncio.sleep(10)

    async def _connect_and_listen(self, market_ids: list[str]) -> None:
        import websockets
        sub_msg = {"type": "subscribe", "markets": market_ids}
        async with websockets.connect(ARB_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            await ws.send(json.dumps(sub_msg))
            logger.info("[ARB] Subscribed to %d markets via WebSocket", len(market_ids))
            async for raw in ws:
                from panopticon_py.utils.process_guard import update_heartbeat
                update_heartbeat("arb_scanner")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._on_message(data)

    async def _on_message(self, data: dict) -> None:
        market_id = data.get("market_id")
        if not market_id or not isinstance(data, dict):
            return

        if "best_ask" in data:
            outcome = data.get("outcome", "YES")
            try:
                price = float(data["best_ask"])
                size = float(data.get("best_ask_size", 0))
            except (ValueError, TypeError):
                return
            self.books[market_id][outcome] = PriceLevel(price=price, size=size)

        await self._check_arb(market_id)

    async def _check_arb(self, market_id: str) -> None:
        book = self.books.get(market_id, {})
        yes = book.get("YES")
        no = book.get("NO")
        if not yes or not no:
            return

        total = yes.price + no.price
        min_depth = min(yes.size, no.size)

        if total < ARB_THRESHOLD and min_depth >= ARB_MIN_DEPTH:
            profit = round((1.0 - total) * 100, 4)
            opp = ArbOpportunity(
                ts=time.time(),
                market_id=market_id,
                yes_price=yes.price,
                no_price=no.price,
                total=total,
                depth=min_depth,
                locked_profit_per_100=profit,
                paper_mode=self.paper_mode,
            )
            self.opportunities_log.append(opp)
            mode_str = "[PAPER]" if self.paper_mode else "[LIVE]"
            logger.info(
                "[ARB_OPP] %s | YES=%.3f NO=%.3f total=%.3f profit/100shares=+$%.2f depth=%d %s",
                market_id, yes.price, no.price, total, profit, int(min_depth), mode_str,
            )


async def main() -> None:
    scanner = ArbScanner(paper_mode=PAPER_MODE)
    await scanner.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    asyncio.run(main())
