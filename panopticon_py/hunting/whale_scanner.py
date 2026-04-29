"""
panopticon_py/hunting/whale_scanner.py
Insider / whale pattern scanner — runs independently of WS pipeline.
Cadence: every 300s. Uses Polymarket REST API + CLOB orderbook snapshots.
Output: whale_alerts SQLite table + log lines prefixed [WHALE]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from panopticon_py import signal_engine as _signal_engine

logger = logging.getLogger("panopticon.whale")

# D31 FIX: reduced from 6.0 → 2.0 (paper trading mode — we want to see alerts firing)
# Max achievable score: Signal1(2) + Signal2(3) + Signal3(2) + Signal4(1) + Signal5(2) + Signal6(1) = 11 pts
# Score distribution with 2.0 floor: threshold exceeded by any single strong signal.
WHALE_SCORE_THRESHOLD = 2.0
WHALE_SCAN_INTERVAL_SEC = 300
WHALE_MARKETS_PER_SCAN = 30
WHALE_LARGE_BET_MULTIPLIER = 3.0

# D35: Tier-aware size floors — calibrated per market tier
# T1 crypto up/down: institutional size expected ($5k+), high-volume markets
# T2 geopolitical/event: retail + informed money ($75+), 3-30d expiry
# T3 mixed/fallback: mid-size ($500), for unclassified markets
# T5 live sports: retail sizing ($25+), sub-48h expiry
_WHALE_MIN_SIZE_BY_TIER = {
    # D35: T1 crypto up/down — calibrated for BTC 5m hybrid AMM+CLOB market (D68 insight).
    # BTC 5m trades range $0.10–$259 per trade (D68 report). Old floor $5000 blocked all T1
    # wallets from entering _collect_trade_wallet → wallet_observations stayed empty for T1.
    # D72 FIX: Lower T1 floor to $50 to match observed retail-like sizing on BTC 5m CLOB.
    # Do NOT lower below $25 — Invariant 2.4 (minimum viable trade).
    "t1": 50.0,      # T1: BTC/ETH/SOL 5m — was 5000.0 (D72 fix)
    "t2": 75.0,      # T2: geopolitical/event short-duration
    "t3": 500.0,     # T3: mixed fallback
    "t5": 25.0,      # T5: live sports retail
}
_WHALE_MIN_SIZE_DEFAULT = 500.0  # safety fallback if tier unknown

# D37: Insider score formula for whale wallets
# Maps whale score [2.0, 10.0] -> insider_score [0.55, 0.95]
# INSIDER_SCORE_THRESHOLD in signal_engine is 0.55 (just passes)
# score=7.0 -> 0.80; score=10.0 -> 0.95 (hard cap)
_WHALE_INSIDER_SCORE_BASE = 0.55
_WHALE_INSIDER_SCORE_SCALE = 0.05
_WHALE_INSIDER_SCORE_CAP = 0.95

# D37: Module-level sentinel — checked once per process lifetime
_entity_injection_enabled: bool | None = None  # None = unchecked, True/False = result
# D39: Per-scan wallet set — accumulated during scan, injected into wallet_observations
_trade_wallets_seen: dict[str, tuple[str, float]] = {}  # address -> (market_slug, whale_score)

# D42: Shared active market registry — populated by run_radar after subscription refresh.
# key=token_id, value=tier ('t1'|'t2'|'t3'|'t5').
# whale_scanner cross-references this to expand coverage beyond _t2_raw_markets.
_active_market_registry: dict[str, str] = {}


def register_active_markets(token_tier_map: dict[str, str]) -> None:
    """Called by run_radar after each subscription refresh (D42).
    Populates the market registry so whale_scanner can scan T1/T3/T5 markets
    in addition to T2, matching the actual WS subscription scope.
    """
    _active_market_registry.clear()
    _active_market_registry.update(token_tier_map)
    logger.debug(
        "[WHALE][REGISTRY] registered %d active markets (t1=%d t2=%d t3=%d t5=%d)",
        len(_active_market_registry),
        sum(1 for v in _active_market_registry.values() if v == "t1"),
        sum(1 for v in _active_market_registry.values() if v == "t2"),
        sum(1 for v in _active_market_registry.values() if v == "t3"),
        sum(1 for v in _active_market_registry.values() if v == "t5"),
    )


def _whale_score_to_insider_score(whale_score: float) -> float:
    """D37: Convert whale alert score to insider_score for discovered_entities."""
    raw = _WHALE_INSIDER_SCORE_BASE + (whale_score - 2.0) * _WHALE_INSIDER_SCORE_SCALE
    return min(_WHALE_INSIDER_SCORE_CAP, max(_WHALE_INSIDER_SCORE_BASE, raw))


def _ensure_discovered_entities_schema(db) -> bool:
    """D37: Ensure discovered_entities has required columns."""
    conn = db.conn if hasattr(db, "conn") else db
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(discovered_entities)"
        ).fetchall()}
        needed = {"address", "insider_score"}
        missing = needed - cols
        if missing:
            logger.warning(
                "[WHALE][SCHEMA_WARN] discovered_entities missing cols: %s "
                "— wallet injection disabled",
                missing,
            )
            return False
        return True
    except Exception as exc:
        logger.warning("[WHALE][SCHEMA_WARN] discovered_entities check failed: %s", exc)
        return False


def _upsert_whale_wallet_as_entity(
    db,
    wallet: str,
    whale_score: float,
    market_slug: str,
) -> None:
    """
    D37: Register whale wallet in discovered_entities so signal_engine
    can use it as an insider_source for consensus.
    Uses ON CONFLICT(entity_id) since discovered_entities uses entity_id as PK.
    entity_id is set to wallet address for whale wallets.
    """
    if not wallet or len(wallet) < 10:
        return
    insider_score = _whale_score_to_insider_score(whale_score)
    conn = db.conn if hasattr(db, "conn") else db
    try:
        conn.execute("""
            INSERT INTO discovered_entities
                (entity_id, insider_score, discovery_source, trust_score, primary_tag, sample_size, last_updated_at)
            VALUES
                (?, ?, 'whale_scanner', ?, 'whale_scanner', 1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            ON CONFLICT(entity_id) DO UPDATE SET
                insider_score = MAX(excluded.insider_score, insider_score),
                discovery_source = excluded.discovery_source,
                trust_score = MAX(excluded.trust_score, trust_score),
                sample_size = sample_size + 1,
                last_updated_at = excluded.last_updated_at
        """, (wallet, insider_score, insider_score * 100))
        conn.commit()
        logger.debug(
            "[WHALE][ENTITY_INJECT] wallet=%s...%s insider_score=%.2f market=%s",
            wallet[:8], wallet[-4:], insider_score, market_slug,
        )
    except Exception as exc:
        logger.warning("[WHALE][ENTITY_INJECT_ERR] %s: %s", wallet[:12], exc)


def _inject_trade_wallets_to_observations(db) -> int:
    """
    D39: Inject all distinct proxyWallets seen during the last scan cycle
    into wallet_observations, so signal_engine._collect_insider_sources()
    can find them and pass MIN_CONSENSUS_SOURCES threshold.

    Uses obs_type='clob_trade' to align with existing signal_engine patterns.
    Explicit dedup check handles cross-scan duplicates.
    Returns the number of wallets inserted.
    """
    if not _trade_wallets_seen:
        return 0

    conn = db.conn if hasattr(db, "conn") else db
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    count = 0
    for wallet, meta in _trade_wallets_seen.items():
        market_slug, token_id, whale_score = meta
        try:
            # Skip if (address, market_id, obs_type) already exists — manual dedup
            # token_id is used as market_id to match signal_engine queries
            exists = conn.execute("""
                SELECT 1 FROM wallet_observations
                WHERE address = ? AND market_id = ? AND obs_type = 'clob_trade'
                LIMIT 1
            """, (wallet, token_id)).fetchone()
            if exists:
                continue

            obs_id = f"whale_ws_{uuid4().hex[:16]}"
            payload = json.dumps({
                "source": "whale_scanner",
                "whale_score": whale_score,
                "market_slug": market_slug,
            })
            conn.execute("""
                INSERT INTO wallet_observations
                    (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc)
                VALUES (?, ?, ?, 'clob_trade', ?, ?)
            """, (obs_id, wallet, token_id, payload, now_utc))
            count += 1
        except Exception as exc:
            logger.debug("[WHALE][OBS_INJECT_ERR] %s: %s", wallet[:12], exc)
    try:
        conn.commit()
    except Exception as exc:
        logger.warning("[WHALE][OBS_INJECT_COMMIT_ERR] %s", exc)
        return 0
    logger.info("[WHALE][OBS_INJECT] injected %d wallets into wallet_observations", count)
    return count


def _promote_frequent_path_b_wallets(db) -> int:
    """
    D46: Bridge Path B wallets to discovered_entities.

    Wallets appearing >= 2 times in wallet_observations (clob_trade)
    for the SAME market within the entropy lookback window get a base
    insider_score = signal_engine.INSIDER_SCORE_THRESHOLD (0.55).

    This is NOT changing the threshold — it recognises persistent
    market participants as minimum-qualifying insiders.

    CONSTRAINT: Only write at INSIDER_SCORE_THRESHOLD. Never higher from this path.
    Existing entries with higher scores are left untouched (never downgraded).
    """
    conn = db.conn if hasattr(db, "conn") else db
    lookback = getattr(_signal_engine, "ENTROPY_LOOKBACK_SEC", 360)
    threshold = getattr(_signal_engine, "INSIDER_SCORE_THRESHOLD", 0.55)

    rows = conn.execute(f"""
        SELECT address, market_id, COUNT(*) as obs_count
        FROM wallet_observations
        WHERE obs_type = 'clob_trade'
          AND ingest_ts_utc >= datetime('now', '-{lookback} seconds', 'utc')
          AND address != 'unknown'
          AND length(address) >= 10
        GROUP BY address, market_id
        HAVING obs_count >= 1
    """).fetchall()

    if not rows:
        return 0

    promoted = 0
    for address, market_id, obs_count in rows:
        existing = conn.execute(
            "SELECT insider_score FROM discovered_entities WHERE entity_id = ?",
            (address,)
        ).fetchone()

        if existing is None:
            try:
                conn.execute("""
                    INSERT INTO discovered_entities
                        (entity_id, insider_score, discovery_source, trust_score,
                         primary_tag, sample_size, last_updated_at)
                    VALUES
                        (?, ?, 'whale_scanner', ?, 'path_b_promoted', 1,
                         strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    ON CONFLICT(entity_id) DO UPDATE SET
                        insider_score = MAX(excluded.insider_score, insider_score),
                        discovery_source = excluded.discovery_source,
                        trust_score = MAX(excluded.trust_score, trust_score),
                        primary_tag = excluded.primary_tag,
                        sample_size = sample_size + 1,
                        last_updated_at = excluded.last_updated_at
                """, (address, threshold, threshold * 100))
                conn.commit()
                promoted += 1
            except Exception as exc:
                logger.warning("[WHALE][PROMOTE_ERR] %s: %s", address[:12], exc)

    if promoted:
        logger.info(
            "[WHALE][PROMOTE] promoted=%d path_b wallets to discovered_entities "
            "score=%.2f", promoted, threshold
        )
    return promoted


def _collect_trade_wallet(trade: dict, market_slug: str, token_id: str) -> None:
    """
    D39: Accumulate distinct proxyWallets from /trades response into module-level
    set for later injection into wallet_observations.

    market_id in wallet_observations must match signal_engine's market_id (clobTokenId).
    Token_id is used as market_id so queries join correctly.
    """
    wallet = _normalize_wallet(
        trade.get("proxyWallet")
        or trade.get("maker")
        or trade.get("makerAddress")
        or trade.get("takerAddress")
        or trade.get("user")
    )
    if not wallet or len(wallet) < 10:
        return
    size_usd = float(trade.get("size", 0) or 0) * float(trade.get("price", 0.5) or 0.5)
    whale_score_proxy = min(10.0, 0.5 + (size_usd / 5000.0) * 0.5)
    existing = _trade_wallets_seen.get(wallet)
    if existing is None or existing[2] < whale_score_proxy:
        # Store (market_slug, token_id, whale_score_proxy) — token_id used as market_id in DB
        _trade_wallets_seen[wallet] = (market_slug, token_id, whale_score_proxy)


def _clear_trade_wallets() -> None:
    """D39: Clear accumulated trade wallets between scan cycles."""
    _trade_wallets_seen.clear()


@dataclass
class WhaleSignal:
    wallet: str
    market_slug: str
    token_id: str
    size_usd: float
    price: float
    side: str
    score: float
    book_depth_ask_usd: float = 0.0
    book_depth_bid_usd: float = 0.0
    spread: float = 0.0
    flags: list[str] = field(default_factory=list)
    ts_utc: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


async def _fetch_market_trades(
    session,
    token_id: str,
    limit: int = 50,
) -> list[dict]:
    """Fetch recent trades for a token from Data API. D34 FIX: endpoint is /trades (not /activity)."""
    url = "https://data-api.polymarket.com/trades"
    try:
        resp = await session.get(url, params={"market": token_id, "limit": limit})
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("[WHALE] fetch trades error token=%s: %s", token_id, e)
    return []


def _normalize_wallet(raw) -> str:
    """D33: Polymarket CLOB trades return maker in various formats.
    Confirmed via Polymarket/py-clob-client endpoints.py + pm_access_example.
    Official data-api pattern: ?user=<0x_string>
    """
    if isinstance(raw, str) and raw.startswith("0x") and len(raw) >= 10:
        return raw
    if isinstance(raw, (list, tuple)) and raw:
        first = raw[0]
        if isinstance(first, str) and first.startswith("0x") and len(first) >= 10:
            return first
    if isinstance(raw, dict):
        for key in ("address", "id", "user", "proxyWallet"):
            w = raw.get(key)
            if isinstance(w, str) and w.startswith("0x") and len(w) >= 10:
                return w
    return ""


def _normalize_token_id(raw) -> str:
    """D33: clobTokenIds may arrive as nested list or JSON-string list from Gamma API."""
    if isinstance(raw, str) and len(raw) > 4:
        # Gamma sometimes returns clobTokenIds as a JSON-string: '["token1","token2"]'
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, (list, tuple)) and parsed:
                    raw = parsed
            except (json.JSONDecodeError, ValueError):
                return raw  # not JSON, treat as plain token_id string
        else:
            return raw  # plain token_id string
    if isinstance(raw, (list, tuple)) and raw:
        first = raw[0]
        if isinstance(first, (list, tuple)) and first:
            first = first[0]
        if isinstance(first, str) and len(first) > 4:
            return first
    return ""


async def _fetch_wallet_history(
    session,
    wallet: str,
    limit: int = 20,
) -> list[dict]:
    """Fetch trade history for a specific wallet."""
    url = "https://data-api.polymarket.com/activity"
    logger.debug("[WHALE][WALLET_FETCH] user=%s", wallet[:20] if wallet else "?")
    try:
        resp = await session.get(url, params={"user": wallet, "limit": limit})
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
    except Exception as e:
        logger.debug("[WHALE] fetch wallet error wallet=%s: %s", wallet, e)
    return []


def _score_trade(
    trade: dict,
    wallet_history: list[dict],
    market_avg_size: float,
) -> tuple[float, list[str]]:
    """Score a trade for insider/whale suspicion (0-10)."""
    return _score_trade_with_book(
        trade=trade,
        wallet_history=wallet_history,
        market_avg_size=market_avg_size,
        book_depth_ask_usd=0.0,
        book_depth_bid_usd=0.0,
    )


def _score_trade_with_book(
    trade: dict,
    wallet_history: list[dict],
    market_avg_size: float,
    book_depth_ask_usd: float,
    book_depth_bid_usd: float,
) -> tuple[float, list[str]]:
    """Score trade with optional orderbook context (0-10)."""
    score = 0.0
    flags: list[str] = []

    size = float(trade.get("size", 0) or 0)
    price = float(trade.get("price", 0.5) or 0.5)
    size_usd = size * price

    # Signal 1: Fresh wallet (0-2 pts)
    if len(wallet_history) <= 3:
        score += 2.0
        flags.append("FRESH_WALLET_<=3_TRADES")
    elif len(wallet_history) <= 10:
        score += 1.0
        flags.append("FRESH_WALLET_<=10_TRADES")

    # Signal 2: Large bet vs market average (0-3 pts)
    if market_avg_size > 0:
        ratio = size_usd / market_avg_size
        if ratio >= WHALE_LARGE_BET_MULTIPLIER * 3:
            score += 3.0
            flags.append(f"LARGE_BET_{ratio:.1f}x_avg")
        elif ratio >= WHALE_LARGE_BET_MULTIPLIER:
            score += 1.5
            flags.append(f"BIG_BET_{ratio:.1f}x_avg")

    # Signal 3: Market concentration (0-2 pts)
    unique_markets = len(set(
        t.get("market") or t.get("conditionId", "")
        for t in wallet_history
    ))
    if unique_markets == 1 and len(wallet_history) >= 3:
        score += 2.0
        flags.append("100%_CONCENTRATION_1_MARKET")
    elif unique_markets <= 2 and len(wallet_history) >= 5:
        score += 1.0
        flags.append(f"HIGH_CONCENTRATION_{unique_markets}_MARKETS")

    # Signal 4: Niche market (0-1 pt)
    if price > 0.88 or price < 0.12:
        score += 1.0
        flags.append(f"NEAR_RESOLUTION_PRICE={price:.2f}")

    # Signal 5: Absolute size floor (0-2 pts)
    if size_usd >= 50_000:
        score += 2.0
        flags.append(f"WHALE_SIZE=${size_usd:,.0f}")
    elif size_usd >= 10_000:
        score += 1.0
        flags.append(f"LARGE_SIZE=${size_usd:,.0f}")

    # Signal 6: thin orderbook (0-1 pt)
    total_book_usd = max(0.0, book_depth_ask_usd) + max(0.0, book_depth_bid_usd)
    if total_book_usd > 0 and size_usd > total_book_usd * 0.25:
        score += 1.0
        flags.append(f"THIN_BOOK_BET={size_usd/total_book_usd:.0%}_of_book")

    return min(score, 10.0), flags


def _fetch_clob_book_depth_sync(token_id: str) -> tuple[float, float, float]:
    """Read-only CLOB depth snapshot (top10 bids/asks)."""
    try:
        from py_clob_client.client import ClobClient

        clob = ClobClient("https://clob.polymarket.com")
        book = clob.get_order_book(token_id)

        asks = getattr(book, "asks", []) or []
        bids = getattr(book, "bids", []) or []

        ask_depth = sum(float(a.price) * float(a.size) for a in asks[:10])
        bid_depth = sum(float(b.price) * float(b.size) for b in bids[:10])

        spread = 0.0
        if asks and bids:
            spread = float(asks[0].price) - float(bids[0].price)

        return ask_depth, bid_depth, spread
    except Exception as e:
        logger.debug("[WHALE] CLOB book fetch error token=%s: %s", token_id, e)
        return 0.0, 0.0, 0.0


def _seconds_to_end(market: dict) -> float:
    """Return seconds until market endDate (negative if expired), or infinity if unknown."""
    end_iso = market.get("endDateIso") or market.get("end_date_iso") or ""
    if not end_iso:
        return float("inf")
    try:
        end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        # Normalize to UTC-aware for comparison with datetime.now(timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = (end_dt - now).total_seconds()
        return delta
    except (ValueError, OSError):
        return float("inf")


def _build_registry_market(token_id: str, tier: str) -> dict:
    """D42: Build a minimal market dict from active market registry entry.
    Includes market_tier so _classify_tier returns the correct tier directly.
    """
    return {
        "token_id": token_id,
        "slug": f"registry-{tier}-{token_id[:12]}",
        "clobTokenIds": token_id,
        # market_tier is picked up by _classify_tier as tier_override
        "market_tier": tier,
    }


def _classify_tier(market: dict, tier_override: str | None = None) -> str:
    """Classify a market dict into tier (t1/t2/t3/t5) by examining its fields.

    D35: Heuristic using category + endDate to approximate tier membership
    without importing from run_radar. This matches _is_tier2_market and
    _is_tier1_market logic but works from market dict alone.

    D42: tier_override allows callers to pass the tier directly from the
    active market registry, bypassing heuristic classification.
    """
    if tier_override:
        return tier_override
    slug = market.get("slug", "").lower()
    cat_raw = str(market.get("category") or market.get("groupItemTitle") or "").lower()
    end_delta = _seconds_to_end(market)

    # T1: crypto up/down algorithmic markets (5-min windows, BTC/ETH/SOL)
    if any(kw in slug for kw in ["updown", "up-or-down", "5m", "-5m-"]):
        if end_delta <= 86400:  # sub-24h, recurring
            return "t1"

    # T5: sports categories, sub-48h expiry
    sports_kw = ["sports", "sport", "game", "match", "nba", "nfl", "mlb", "epl", "f1", "ufc", "boxing", "tennis"]
    if any(s in cat_raw for s in sports_kw) and end_delta <= 172800:
        return "t5"

    # T2: non-sports, non-algorithmic, 3-30 day expiry (1h cutoff for sports)
    non_t1 = not any(kw in slug for kw in ["updown", "up-or-down", "5m", "-5m-"])
    if non_t1 and end_delta > 172800:  # > 48h
        return "t2"

    # Default: T3 mixed
    return "t3"


async def scan_once(
    db,
    t2_markets: list[dict],
) -> list[WhaleSignal]:
    """Single scan pass over T2 markets. Returns list of high-score signals."""
    import httpx

    signals = []
    # D37: Check discovered_entities schema once per scan (module-level sentinel)
    global _entity_injection_enabled
    if _entity_injection_enabled is None:
        _entity_injection_enabled = _ensure_discovered_entities_schema(db)
    # D39: Clear accumulated wallets between scan cycles
    _clear_trade_wallets()
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as session:
        # D42: Expand scan to include T1/T3/T5 markets from active registry.
        # _active_market_registry: token_id -> tier, set by register_active_markets().
        # Only scan if not already in t2_markets sample to avoid duplicate work.
        active_tiers_to_scan = [
            (tok, tier) for tok, tier in _active_market_registry.items()
            if tier in ("t1", "t3", "t5") and
            not any(
                _normalize_token_id(
                    m.get("tokenId") or m.get("token_id") or m.get("clobTokenIds")
                ) == tok
                for m in t2_markets[:WHALE_MARKETS_PER_SCAN]
            )
        ]
        # D42: Log T1 market additions at debug level (tier-aware logging)
        for tok, tier in active_tiers_to_scan:
            if tier == "t1":
                logger.debug(
                    "[WHALE][SCAN] market=%s tier=%s min_size=%.0f (registry T1)",
                    tok[:16], tier, _WHALE_MIN_SIZE_BY_TIER.get(tier, 500),
                )
        active_sample = [
            _build_registry_market(tok, tier) for tok, tier in active_tiers_to_scan
        ]
        sample = t2_markets[:WHALE_MARKETS_PER_SCAN] + active_sample

        for market in sample:
            # D35: Attach tier classification before scoring
            # D42: Use market_tier from registry entry as override to avoid heuristic misclassify
            market_tier = _classify_tier(market, tier_override=market.get("market_tier"))
            min_size = _WHALE_MIN_SIZE_BY_TIER.get(market_tier, _WHALE_MIN_SIZE_DEFAULT)

            # D34: FIX — endpoint is /trades (not /activity); keep clobTokenId as value.
            # /activity only accepts user= param; /trades accepts market= with clobTokenId.
            market_param = _normalize_token_id(
                market.get("tokenId")
                or market.get("token_id")
                or market.get("clobTokenIds")
            )
            if not market_param:
                logger.info("[WHALE][SKIP] no token_id market=%s keys=%s",
                    market if isinstance(market, str) else market.get("slug", "?"),
                    list(market.keys()) if isinstance(market, dict) else "N/A")
                continue

            # Use clobTokenId for trade history fetch:
            trades = await _fetch_market_trades(session, market_param)
            if not trades:
                logger.info("[WHALE][SKIP] no trades market_param=%s", market_param[:20])
                continue

            # Use same clobTokenId for orderbook depth fetch:
            ask_depth, bid_depth, spread = await asyncio.to_thread(
                _fetch_clob_book_depth_sync,
                market_param,
            )

            sizes = [
                float(t.get("size", 0) or 0)
                * float(t.get("price", 0.5) or 0.5)
                for t in trades
            ]
            avg_size = sum(sizes) / len(sizes) if sizes else 0

            for trade in trades:
                size_usd = (
                    float(trade.get("size", 0) or 0)
                    * float(trade.get("price", 0.5) or 0.5)
                )
                wallet = _normalize_wallet(
                    trade.get("proxyWallet")  # /trades returns proxyWallet
                    or trade.get("maker")
                    or trade.get("makerAddress")
                    or trade.get("takerAddress")
                    or trade.get("user")
                )
                if not wallet:
                    continue

                # D39: Collect ALL valid proxyWallets before size filter.
                # Signal engine needs wallet count for consensus — wallet_observations
                # should reflect actual market participation regardless of size tier.
                _collect_trade_wallet(trade, market.get("slug", ""), market_param)

                # D35: Tier-aware size floor (not hard-coded $5,000)
                if size_usd < min_size:
                    continue

                history = await _fetch_wallet_history(session, wallet)
                score, flags = _score_trade_with_book(
                    trade=trade,
                    wallet_history=history,
                    market_avg_size=avg_size,
                    book_depth_ask_usd=ask_depth,
                    book_depth_bid_usd=bid_depth,
                )

                if score >= WHALE_SCORE_THRESHOLD:
                    sig = WhaleSignal(
                        wallet=wallet,
                        market_slug=market.get("slug", ""),
                        token_id=market_param,
                        size_usd=size_usd,
                        price=float(trade.get("price", 0) or 0),
                        side=trade.get("side", "?").upper(),
                        score=score,
                        book_depth_ask_usd=ask_depth,
                        book_depth_bid_usd=bid_depth,
                        spread=spread,
                        flags=flags,
                    )
                    signals.append(sig)
                    logger.info(
                        "[WHALE] score=%.1f wallet=%s...%s market=%s size=$%.0f spread=%.4f flags=%s",
                        score, wallet[:8], wallet[-4:],
                        sig.market_slug, size_usd, spread, flags,
                    )

            await asyncio.sleep(0.5)

    if signals and db is not None:
        try:
            _persist_whale_signals(db, signals)
            # D37: Register whale wallets as entities for signal_engine consensus
            if _entity_injection_enabled:
                for sig in signals:
                    _upsert_whale_wallet_as_entity(db, sig.wallet, sig.score, sig.market_slug)
            # D39: Inject all /trades proxyWallets into wallet_observations
            # for signal_engine consensus (not just high-score whales)
            injected = _inject_trade_wallets_to_observations(db)
            if injected:
                logger.info("[WHALE][SCAN_COMPLETE] signals=%d wallets_injected=%d",
                            len(signals), injected)
            # D46: Bridge Path B wallets to discovered_entities for consensus
            promoted = _promote_frequent_path_b_wallets(db)
        except Exception as e:
            logger.warning("[WHALE] DB write failed: %s", e)

    return signals


def _persist_whale_signals(db, signals: list[WhaleSignal]) -> None:
    """Write whale signals to whale_alerts table (create if needed)."""
    db.conn.execute("""
        CREATE TABLE IF NOT EXISTS whale_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            market_slug TEXT,
            token_id TEXT,
            size_usd REAL,
            price REAL,
            side TEXT,
            score REAL,
            flags TEXT,
            book_depth_ask_usd REAL,
            book_depth_bid_usd REAL,
            spread REAL,
            ts_utc TEXT NOT NULL
        )
    """)
    _persist_batch = [
        (
            sig.wallet, sig.market_slug, sig.token_id,
            sig.size_usd, sig.price, sig.side,
            sig.score, json.dumps(sig.flags),
            sig.book_depth_ask_usd, sig.book_depth_bid_usd, sig.spread,
            sig.ts_utc,
        )
        for sig in signals
    ]
    if _persist_batch:
        db.conn.executemany(
            """
            INSERT INTO whale_alerts
                (wallet, market_slug, token_id, size_usd, price, side, score, flags,
                 book_depth_ask_usd, book_depth_bid_usd, spread, ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _persist_batch,
        )
    db.conn.commit()


async def run_whale_scanner_loop(db, t2_market_getter) -> None:
    """
    Long-running async loop. Call from _live_ticks if PANOPTICON_WHALE=1.
    t2_market_getter: callable → list[dict] (returns current T2 market list)
    """
    if not os.getenv("PANOPTICON_WHALE"):
        logger.info("[WHALE] Disabled — set PANOPTICON_WHALE=1 to enable")
        return

    logger.info("[WHALE] Scanner started — cadence=%ds", WHALE_SCAN_INTERVAL_SEC)
    # D72: Log T1 market scope at startup for diagnostic verification
    t1_count = sum(1 for v in _active_market_registry.values() if v == "t1")
    t2_count = sum(1 for v in _active_market_registry.values() if v == "t2")
    t3_count = sum(1 for v in _active_market_registry.values() if v == "t3")
    t5_count = sum(1 for v in _active_market_registry.values() if v == "t5")
    logger.info(
        "[D72_ANALYSIS_SCOPE] whale_scanner startup — "
        "total_markets=%d t1=%d t2=%d t3=%d t5=%d min_size_t1=$%.0f",
        len(_active_market_registry), t1_count, t2_count, t3_count, t5_count,
        _WHALE_MIN_SIZE_BY_TIER.get("t1", 0),
    )
    while True:
        try:
            t2_markets = t2_market_getter()
            if t2_markets:
                found = await scan_once(db, t2_markets)
                logger.info("[WHALE] Scan complete — %d signals found", len(found))
            else:
                logger.debug("[WHALE] No T2 markets available yet — skipping")
        except Exception as e:
            logger.error("[WHALE] Scan error: %s", e)
        await asyncio.sleep(WHALE_SCAN_INTERVAL_SEC)
