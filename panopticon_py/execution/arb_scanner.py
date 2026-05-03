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

PROCESS_VERSION = "v0.5.9-D148"   # ← AGENT: bump on every change  # D138-P0: +top-level exception + crash manifest + D138-P1: +heartbeat_loop warning  # D139-P0: +WS reconnection loop  # D140-P0: acquire_singleton in __main__ (not run())  # D141-P2: re-fetch token_ids on each WS reconnect  # D142-P1: sync self._token_ids on reconnect  # D142-P2: fetch_t5_token_ids async httpx  # D146-P0: crash-protection (wait_for timeouts, run() restructure, ping_timeout, crash_time manifest)  # D148-1: arb_stats table added to DB schema  # D148-2: _flush_stats() writer + opp/reconnect counters + process_guard import in _on_message

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


async def fetch_t5_token_ids(
    session: httpx.AsyncClient | None = None,
) -> tuple[list[str], list[str]]:
    """
    D119-P0: Fetch active T5 sports CLOB token_ids from Gamma API.

    D142-P2: Uses AsyncClient when ``session`` is provided (reuse ArbScanner pool).
    Otherwise creates a temporary client — avoids blocking the asyncio event loop.

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
    own_session = session is None
    if own_session:
        session = httpx.AsyncClient(timeout=15.0)
    assert session is not None  # for type checker

    markets: object | None = None
    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        url = f"{base}{path}?closed=false&limit=500"
        resp = await session.get(url)
        try:
            resp.raise_for_status()
            markets = resp.json()
        finally:
            await resp.aclose()
    except Exception as e:
        logger.warning("[ARB] Failed to fetch T5 markets from Gamma: %s", e)
        return [], []
    finally:
        if own_session:
            await session.aclose()

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
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    # D148-2: Stats persistence
    _db: object = field(default=None, init=False, repr=False)
    _reconnect_count: int = field(default=0, init=False)
    _opp_count_total: int = field(default=0, init=False)
    _last_flush_ts: float = field(default_factory=time.time, init=False)

    # D137-1: 30s fixed heartbeat — independent of WS message frequency
    def _heartbeat_loop(self) -> None:
        """D137: Writes heartbeat every 30s regardless of WS message rate."""
        _hb_logger = logging.getLogger("arb_scanner.heartbeat")
        while not self._stop_event.is_set():
            try:
                from panopticon_py.utils.process_guard import update_heartbeat
                update_heartbeat("arb_scanner")
            except Exception as exc:
                # D138: log instead of silent pass — unknown errors must be visible
                _hb_logger.warning("[ARB_HB] update_heartbeat failed: %s", exc)
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
        D146-P0-2: Hard 15s timeout — fee query failure must never block the main loop.
        """
        try:
            rates = await asyncio.wait_for(
                self._fetch_fee_rates(token_ids),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[ARB_FEE] Fee rate query timed out (15s) — skipping filter, using all %d tokens",
                len(token_ids),
            )
            return token_ids
        except Exception as e:
            logger.warning("[ARB_FEE] Fee filter error: %s — using unfiltered list", e)
            return token_ids

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

    # D148-2: _flush_stats is called from _on_message on 60s tick.
    # DB write is fire-and-forget — failure must never impact WS loop.
    async def _flush_stats(self) -> None:
        """Persist current stats snapshot to arb_stats DB table. Silent fail."""
        try:
            if self._db is None:
                from panopticon_py.db import ShadowDB
                self._db = ShadowDB()
        except Exception as e:
            logger.debug("[ARB_STATS] DB init failed: %s", e)
            return

        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            total_updates = sum(self._update_counter.values())
            active_tokens = len([v for v in self._update_counter.values() if v > 0])
            n_subscribed = len(self._token_ids) if hasattr(self, "_token_ids") and self._token_ids else 0
            opp_count_1h = sum(
                1 for o in self.opportunities_log
                if (time.time() - o.ts) < 3600
            )
            best_profit = max(
                (o.locked_profit_per_100 for o in self.opportunities_log),
                default=0.0,
            )
            tokens_excluded = len([tid for tid, bps in self._fee_rates.items() if bps > 300])
            tokens_total = len(self._original_token_ids)

            self._db.conn.execute(
                """INSERT INTO arb_stats
                   (ts_utc, ws_connected, tokens_subscribed, active_tokens,
                    total_updates, reconnect_count, opp_count_total, opp_count_1h,
                    best_profit, tokens_total, tokens_kept, tokens_excluded)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now_iso, 1, n_subscribed, active_tokens,
                 total_updates, self._reconnect_count, self._opp_count_total,
                 opp_count_1h, best_profit, tokens_total,
                 tokens_total - tokens_excluded, tokens_excluded),
            )
            self._db.conn.execute(
                "DELETE FROM arb_stats WHERE id NOT IN "
                "(SELECT id FROM arb_stats ORDER BY ts_utc DESC LIMIT 1440)"
            )
            self._db.conn.commit()
            self._last_flush_ts = time.time()
            # D148-2: reset _last_stats_log so _on_message 60s guard doesn't re-flush
            self._last_stats_log = time.time()
        except Exception as e:
            logger.debug("[ARB_STATS] write failed: %s", e)

    async def run(self) -> None:
        """
        D119-P0: Main entry point.
        D146-P0-3: Restructured — heartbeat starts immediately, Gamma fetch has a 20s
        hard timeout so startup never blocks longer than 35s total. WS listener enters
        regardless of whether initial token fetch succeeded; _connect_and_listen refreshes
        tokens on each reconnect.
        """
        # D137-1: Start 30s fixed heartbeat FIRST — must be writing before any blocking call
        self._start_heartbeat()

        # D121-2: Create HTTP session + semaphore
        self._http_session = httpx.AsyncClient(timeout=10.0)
        self._fee_semaphore = asyncio.Semaphore(10)

        logger.info("[ARB] Starting ArbScanner paper_mode=%s auto_refresh=%ds",
                    PAPER_MODE, self._refresh_interval)

        # ── Step 1: Initial token discovery with hard 20s timeout ─────────────────
        try:
            token_ids, market_ids = await asyncio.wait_for(
                fetch_t5_token_ids(session=self._http_session),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            logger.warning("[ARB] Initial Gamma fetch timed out (20s) — starting WS with empty token list")
            token_ids, market_ids = [], []
        except Exception as e:
            logger.warning("[ARB] Initial Gamma fetch failed: %s — starting WS with empty token list", e)
            token_ids, market_ids = [], []

        current_token_ids: list[str] = list(token_ids)

        if current_token_ids:
            self._token_ids = current_token_ids
            logger.info("[ARB] Discovered %d T5 token_ids (%d markets) via Gamma API",
                        len(current_token_ids), len(market_ids))
            # D146-P0-2: _apply_fee_filter has internal 15s timeout — safe to await directly
            filtered = await self._apply_fee_filter(current_token_ids)
            if not filtered:
                logger.warning("[ARB] All tokens filtered by fee rate — keeping unfiltered list to avoid empty subscription")
            else:
                current_token_ids = filtered
            self._original_token_ids = current_token_ids
            # D119-P0: [ARB_INIT] format validation (0x + 64 hex chars = 66 total)
            sample = current_token_ids[:3]
            all_valid = all(len(t) == 66 and t.startswith("0x") for t in current_token_ids)
            invalid = [t for t in current_token_ids if not (len(t) == 66 and t.startswith("0x"))]
            logger.info("[ARB_INIT] token_ids=%d sample=%s all_valid_format=%s",
                        len(current_token_ids), sample, all_valid)
            if invalid:
                logger.warning("[ARB_INIT] %d invalid token_ids: %s", len(invalid), invalid[:5])
        else:
            logger.warning("[ARB] No T5 tokens at startup — WS starts with empty list; refresh on first reconnect")

        # Start background fee refresh loop (once per process lifetime)
        if not self._refresh_started:
            self._refresh_started = True
            asyncio.create_task(self._fee_rate_refresh_loop())

        # ── Step 2: WS listener — handles all reconnects + token refresh internally ─
        while True:
            try:
                await self._connect_and_listen(current_token_ids)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("[ARB] Error in run loop: %s; reconnecting in 10s…", e)
                await asyncio.sleep(10)

    async def _connect_and_listen(self, token_ids: list[str]) -> None:
        """
        D139: Wraps WS connection with exponential-backoff reconnection loop.
        D141: On each reconnect, re-fetches token_ids from Gamma API to stay current
        with market state changes (new markets, settled markets).
        Disconnection is normal (server-side close, network drop), not a crash.
        """
        import websockets

        backoff = 5.0
        max_backoff = 120.0
        current_token_ids = token_ids  # initial list; updated on each reconnect

        while not self._stop_event.is_set():
            self._reconnect_count += 1   # D148-2: track reconnects
            try:
                sub_msg = {"type": "subscribe", "assets_ids": current_token_ids}
                async with websockets.connect(
                    ARB_WS_URL,
                    ping_interval=20,
                    ping_timeout=30,   # D146-P0-4: no-pong for 30s → ConnectionClosed
                    close_timeout=10,  # D146-P0-4: closing handshake hard limit
                ) as ws:
                    self._ws = ws
                    await ws.send(json.dumps(sub_msg))
                    backoff = 5.0  # reset on successful connect
                    logger.info(
                        "[ARB] WS connected, subscribed to %d token_ids",
                        len(current_token_ids),
                    )
                    # D148-2: Flush stats on connection so arb_stats is populated
                    # even before any WS messages arrive. Don't await — let it run
                    # in background so WS receive loop isn't blocked.
                    asyncio.create_task(self._flush_stats())
                    self._last_flush_ts = time.time()  # reset 60s interval from connect time
                    self._last_stats_log = time.time()  # D148-2: align 60s tick with flush on connect

                    async for raw in ws:
                        from panopticon_py.utils.process_guard import update_heartbeat
                        update_heartbeat("arb_scanner")
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(data, list):
                            for item in data:
                                if isinstance(item, dict):
                                    await self._on_message(item)
                        elif isinstance(data, dict):
                            await self._on_message(data)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    raise
                logger.warning(
                    "[ARB_WS] Disconnected: %s — reconnecting in %.0fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                # D141: Re-fetch and re-filter token_ids on every reconnect so the
                # subscription stays aligned with current market state.  If this fails,
                # fall back to the previous list — WS reconnection is more important
                # than refreshing the market list.
                # D146-P0-1: Hard 20s timeout prevents Gamma hang from blocking reconnect.
                try:
                    fresh_ids, fresh_markets = await asyncio.wait_for(
                        fetch_t5_token_ids(session=self._http_session),
                        timeout=20.0,
                    )
                    if fresh_ids:
                        # _apply_fee_filter has its own 15s internal timeout
                        filtered = await self._apply_fee_filter(fresh_ids)
                        if filtered:
                            old_count = len(current_token_ids)
                            current_token_ids = filtered
                            self._original_token_ids = filtered
                            self._token_ids = filtered   # D142: keep stats counter in sync
                            logger.info(
                                "[ARB_WS] Token refresh on reconnect: %d → %d",
                                old_count, len(current_token_ids),
                            )
                except Exception as refresh_exc:
                    logger.debug(
                        "[ARB_WS] Token refresh on reconnect failed: %s — "
                        "using previous token list (%d tokens)",
                        refresh_exc, len(current_token_ids),
                    )

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
            # D148-2: persist stats snapshot to DB
            await self._flush_stats()

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
            self._opp_count_total += 1   # D148-2: track total opportunities
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
    _main_logger = logging.getLogger("arb_scanner.main")
    _main_logger.info("[ARB_MAIN] arb_scanner %s starting", PROCESS_VERSION)

    # D140: acquire_singleton at process entry point (not inside run()).
    # This lets singleton conflicts exit cleanly with sys.exit(0) before
    # the asyncio event loop starts, preventing zombie heartbeats.
    from panopticon_py.utils.process_guard import acquire_singleton
    try:
        acquire_singleton("arb_scanner", PROCESS_VERSION)
    except Exception as exc:
        _main_logger.error("[ARB_SINGLETON] Failed: %s — another instance running?", exc)
        import sys
        sys.exit(0)   # exit 0 = clean, won't trigger watchdog restart

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _main_logger.info("[ARB_EXIT] Interrupted by user (KeyboardInterrupt)")
    except Exception as exc:
        # D138-P0: Capture all unhandled exceptions to ensure crash has trace
        _main_logger.exception("[ARB_CRASH] Unhandled exception in main(): %s", exc)
        try:
            from panopticon_py.utils.process_guard import _read_manifest, _write_manifest
            manifest = _read_manifest()
            if "arb_scanner" in manifest:
                manifest["arb_scanner"]["status"] = "crashed"
                manifest["arb_scanner"]["crash_reason"] = str(exc)[:200]
                manifest["arb_scanner"]["crash_time"] = datetime.now(timezone.utc).isoformat()
                _write_manifest("arb_scanner", manifest["arb_scanner"])
        except Exception as me:
            _main_logger.warning("[ARB_CRASH] manifest write failed: %s", me)
        import sys
        sys.exit(1)
    finally:
        _main_logger.info("[ARB_EXIT] arb_scanner main() exited cleanly")
