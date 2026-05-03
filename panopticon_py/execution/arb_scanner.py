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
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PROCESS_VERSION = "v0.5.2-D137"   # ← AGENT: bump on every change  # D137-1: +30s fixed heartbeat thread

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


async def fetch_t5_token_ids() -> tuple[list[str], list[str]]:
    """
    D119-P0: Fetch active T5 sports CLOB token_ids from Gamma API.

    Returns:
        (token_ids, market_ids) — both lists of strings.
        - token_ids: used for CLOB WS subscription (clobTokenIds[0])
        - market_ids: condition_id for logging/debugging

    Polymarket CLOB WS subscription field is "assets_ids" = clobTokenIds array,
    NOT the market condition_id. Using wrong ID results in silent WS drop.
    """
    token_ids: list[str] = []
    market_ids: list[str] = []
    seen_tokens: set[str] = set()
    seen_markets: set[str] = set()
    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        url = f"{base}{path}?closed=false&limit=500"
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        markets = resp.json()
    except Exception as e:
        logger.warning("[ARB] Failed to fetch T5 markets from Gamma: %s", e)
        return [], []

    if not isinstance(markets, list):
        return [], []

    for m in markets:
        if not isinstance(m, dict):
            continue
        if _is_tier5_sports_market(m):
            # Extract CLOB token IDs (NOT the condition_id market id)
            raw_tids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(raw_tids, str):
                try:
                    raw_tids = json.loads(raw_tids)
                except json.JSONDecodeError:
                    raw_tids = []
            elif not isinstance(raw_tids, list):
                raw_tids = [raw_tids]

            # Use first token (YES outcome) for subscription
            tid_raw = raw_tids[0] if raw_tids else None
            # D119-P0: Gamma API returns token_id as decimal integer string
            # (e.g. "43891259347116330522865864075089973515827852946539612217753302847337982135578")
            # JSON parser may treat it as str if it overflows int precision.
            # Convert to 0x hex for CLOB WS subscription.
            if isinstance(tid_raw, int):
                tid_str = "0x" + hex(tid_raw)[2:].zfill(64)
            elif isinstance(tid_raw, str) and tid_raw.isdigit():
                tid_str = "0x" + hex(int(tid_raw))[2:].zfill(64)
            elif isinstance(tid_raw, str) and tid_raw.startswith("0x"):
                tid_str = tid_raw  # already hex
            else:
                tid_str = str(tid_raw).strip() if tid_raw is not None else ""
            mid = str(m.get("id") or m.get("market_id") or "")

            if tid_str and tid_str not in seen_tokens:
                seen_tokens.add(tid_str)
                token_ids.append(tid_str)
            if mid and mid not in seen_markets:
                seen_markets.add(mid)
                market_ids.append(mid)

    return token_ids, market_ids


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
    # D120-P2: Track per-token book update frequency
    _update_counter: dict[str, int] = field(default_factory=dict)
    _ws: object = field(default=None, init=False, repr=False)
    _refresh_interval: int = field(default=60, init=False)
    _last_stats_log: float = field(default_factory=time.time, init=False)
    # D121-2: Fee rate filter
    _original_token_ids: list[str] = field(default_factory=list, init=False)
    _fee_rates: dict[str, int] = field(default_factory=dict, init=False)
    _http_session: httpx.AsyncClient | None = field(default=None, init=False, repr=False)
    _fee_semaphore: asyncio.Semaphore = field(init=False, repr=False)
    _refresh_started: bool = field(default=False, init=False)
    _stop_event: threading.Event = field(default=threading.Event, init=False, repr=False)

    # D137-1: 30s fixed heartbeat — independent of WS message frequency
    def _heartbeat_loop(self) -> None:
        """D137: Writes heartbeat every 30s regardless of WS message rate."""
        while not self._stop_event.is_set():
            try:
                from panopticon_py.utils.process_guard import update_heartbeat
                update_heartbeat("arb_scanner")
            except Exception:
                pass
            self._stop_event.wait(timeout=30)

    def _start_heartbeat(self) -> None:
        hb = threading.Thread(target=self._heartbeat_loop, name="arb-heartbeat", daemon=True)
        hb.start()

    async def _fetch_fee_rates(self, token_ids: list[str]) -> dict[str, int]:
        """
        D121-2: Batch query fee rates for given token_ids.
        Uses Semaphore(10) to limit concurrent requests.
        Defaults to 0 bps on failure (don't exclude on error).
        """
        if not token_ids:
            return {}
        if self._fee_semaphore is None:
            self._fee_semaphore = asyncio.Semaphore(10)

        async def _query_one(tid: str) -> tuple[str, int]:
            async with self._fee_semaphore:
                try:
                    url = f"https://clob.polymarket.com/fee-rate?token_id={tid}"
                    resp = await self._http_session.get(url, timeout=5.0)
                    try:
                        if resp.status_code == 404:
                            # CLOB V2: fee not explicitly set — use sports default (30 bps)
                            return tid, 30
                        if resp.status_code == 401:
                            logger.warning(
                                "[ARB_FEE] 401 on fee-rate for %s — need CLOB auth header?",
                                tid[:20],
                            )
                            return tid, 30  # default to sports rate on auth error
                        data = resp.json()
                        # V2 format: {"base_fee": 30}, legacy: {"fee_rate_bps": 30}
                        bps = int(data.get("base_fee") or data.get("fee_rate_bps") or 30)
                        # Brief pause between batches to avoid rate limit
                        await asyncio.sleep(0.1)
                        return tid, bps
                    finally:
                        await resp.aclose()
                except Exception as e:
                    logger.debug("[ARB_FEE] query failed for %s: %s", tid[:20], e)
                    return tid, 0

        logger.info("[ARB_FEE] Querying fee rates for %d tokens", len(token_ids))
        results = await asyncio.gather(*[_query_one(tid) for tid in token_ids])
        rates = dict(results)
        logger.info("[ARB_FEE] Got %d fee rates", len(rates))
        return rates

    async def _apply_fee_filter(self, token_ids: list[str]) -> list[str]:
        """
        D121-2: Filter out tokens with fee_rate_bps > 300.
        Stores fee rates in self._fee_rates for observability.
        """
        rates = await self._fetch_fee_rates(token_ids)
        self._fee_rates = rates

        excluded = [tid for tid, bps in rates.items() if bps > 300]
        if excluded:
            logger.info(
                "[ARB_FEE] Excluded %d tokens with fee_rate_bps > 300: %s",
                len(excluded),
                [tid[:20] for tid in excluded[:5]],
            )

        kept = [tid for tid in token_ids if rates.get(tid, 0) <= 300]
        logger.info(
            "[ARB_FEE_SUMMARY] total=%d kept=%d excluded=%d",
            len(token_ids), len(kept), len(excluded),
        )
        return kept

    async def _fee_rate_refresh_loop(self) -> None:
        """
        D121-2: Refresh fee rates every 6 hours and reapply filter.
        Runs as a background task inside the main loop.
        """
        while True:
            await asyncio.sleep(6 * 3600)
            if self._original_token_ids:
                logger.info("[ARB_FEE_REFRESH] Refreshing fee rates for %d tokens", len(self._original_token_ids))
                filtered = await self._apply_fee_filter(self._original_token_ids)
                self._original_token_ids = filtered
                logger.info("[ARB_FEE_REFRESH] Done, active tokens: %d", len(filtered))

    async def run(self) -> None:
        """
        D119-P0: Main entry point.
        Auto-discovers T5 token_ids from Gamma API (CLOB WS uses token_id, NOT condition_id).
        Refreshes every 60s.
        """
        from panopticon_py.utils.process_guard import acquire_singleton
        acquire_singleton("arb_scanner", PROCESS_VERSION)

        # D137-1: Start 30s fixed heartbeat thread immediately after singleton
        self._start_heartbeat()

        # D121-2: Create HTTP session for fee rate queries
        self._http_session = httpx.AsyncClient(timeout=10.0)
        self._fee_semaphore = asyncio.Semaphore(10)

        logger.info("[ARB] Starting ArbScanner paper_mode=%s auto_refresh=%ds",
                    PAPER_MODE, self._refresh_interval)
        current_token_ids: list[str] = []
        current_market_ids: list[str] = []

        while True:
            try:
                # ── Step 1: Auto-discover T5 token_ids (CLOB WS format) ───────────
                token_ids, market_ids = await fetch_t5_token_ids()
                if token_ids:
                    self._token_ids = token_ids  # store for stats tracking
                    current_token_ids = token_ids
                    current_market_ids = market_ids
                    logger.info("[ARB] Discovered %d T5 token_ids (%d markets) via Gamma API",
                                len(current_token_ids), len(current_market_ids))
                    # D121-2: Apply fee rate filter before subscribing
                    try:
                        current_token_ids = await self._apply_fee_filter(current_token_ids)
                        if not current_token_ids:
                            logger.warning("[ARB] All tokens filtered out by fee rate; retrying in 30s…")
                            await asyncio.sleep(30)
                            continue
                        # D121-2: Store for 6h refresh loop
                        self._original_token_ids = current_token_ids
                        # Start background refresh loop (only once)
                        if not hasattr(self, "_refresh_started") or not self._refresh_started:
                            self._refresh_started = True
                            asyncio.create_task(self._fee_rate_refresh_loop())
                    except Exception as e:
                        logger.warning("[ARB_FEE] Fee filter failed, using unfiltered list: %s", e)
                        self._original_token_ids = current_token_ids
                        if not hasattr(self, "_refresh_started") or not self._refresh_started:
                            self._refresh_started = True
                            asyncio.create_task(self._fee_rate_refresh_loop())
                    # D119-P0: [ARB_INIT] format validation
                    # Valid CLOB token_id = "0x" + 64 hex chars = 66 total chars
                    sample = current_token_ids[:3]
                    all_valid = all(len(t) == 66 and t.startswith("0x") for t in current_token_ids)
                    invalid = [t for t in current_token_ids if not (len(t) == 66 and t.startswith("0x"))]
                    logger.info(
                        "[ARB_INIT] token_ids=%d sample=%s all_valid_format=%s",
                        len(current_token_ids), sample, all_valid,
                    )
                    if invalid:
                        logger.warning("[ARB_INIT] %d invalid token_ids: %s", len(invalid), invalid[:5])
                elif not current_token_ids:
                    logger.warning("[ARB] No T5 token_ids found; retrying in 30s…")
                    await asyncio.sleep(30)
                    continue

                # ── Step 2: Run WS listener with CLOB token_ids ────────────────────
                await self._connect_and_listen(current_token_ids)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ARB] Error in run loop: %s; reconnecting in 10s…", e)
                await asyncio.sleep(10)

    async def _connect_and_listen(self, token_ids: list[str]) -> None:
        import websockets
        # D119-P0: Polymarket CLOB WS uses "assets_ids" field (clobTokenIds, NOT condition_ids)
        sub_msg = {"type": "subscribe", "assets_ids": token_ids}
        async with websockets.connect(ARB_WS_URL, ping_interval=20) as ws:
            self._ws = ws
            await ws.send(json.dumps(sub_msg))
            logger.info("[ARB] Subscribed to %d token_ids via WebSocket (assets_ids format)", len(token_ids))
            async for raw in ws:
                from panopticon_py.utils.process_guard import update_heartbeat
                update_heartbeat("arb_scanner")
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # D120: Polymarket WS sends both dict messages and list batches
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            await self._on_message(item)
                elif isinstance(data, dict):
                    await self._on_message(data)

    async def _on_message(self, data: dict) -> None:
        # D120: Guard against missing market_id
        market_id = data.get("market_id") if isinstance(data, dict) else None
        if not market_id:
            return

        # D120-P2: Track book update frequency per token
        token_id = data.get("asset_id") or market_id
        if token_id:
            self._update_counter[token_id] = self._update_counter.get(token_id, 0) + 1

        if "best_ask" in data:
            outcome = data.get("outcome", "YES")
            try:
                price = float(data["best_ask"])
                size = float(data.get("best_ask_size", 0))
            except (ValueError, TypeError):
                return
            self.books[market_id][outcome] = PriceLevel(price=price, size=size)

        # D120-P2: Log stats summary every 60 seconds
        now = time.time()
        if now - self._last_stats_log >= 60.0:
            total_updates = sum(self._update_counter.values())
            active_tokens = len([v for v in self._update_counter.values() if v > 0])
            total_subscribed = getattr(self, "_token_ids", None)
            n_subscribed = len(total_subscribed) if total_subscribed else 0
            logger.info(
                "[ARB_STATS] total_updates=%d active_tokens=%d/%d elapsed_s=%.0f",
                total_updates, active_tokens, n_subscribed, now - self._last_stats_log,
            )
            self._last_stats_log = now

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
    """D135: Wrap run() to catch all exceptions and log exit reason before terminating."""
    from panopticon_py.utils.process_guard import update_heartbeat
    scanner = ArbScanner(paper_mode=PAPER_MODE)
    try:
        await scanner.run()
    except asyncio.CancelledError:
        logger.info("[ARB_EXIT] CancelledError — graceful shutdown")
        scanner._stop_event.set()
        raise
    except Exception as e:
        logger.error("[ARB_EXIT] Unhandled exception: %s — process will exit", e, exc_info=True)
        try:
            update_heartbeat("arb_scanner")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[ARB_EXIT] KeyboardInterrupt")
    except Exception as e:
        logger.error("[ARB_FATAL] %s", e)
