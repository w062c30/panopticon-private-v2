"""Asyncio radar: WebSocket (or synthetic) → entropy window → shadow hits in SQLite."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.entropy_window import EntropyWindow
from panopticon_py.signal_engine import SignalEvent
from panopticon_py.hunting.trade_aggregate import aggregate_taker_sweeps, cross_wallet_burst_cluster
from panopticon_py.ingestion.order_reconstruction_engine import close_stale_orders
from panopticon_py.load_env import load_repo_env
from panopticon_py.analysis.insider_pattern import compute_pattern_score
from panopticon_py.series.event_series import classify_oracle_risk, ORACLE_RISK_HIGH
from panopticon_py.time_utils import normalize_external_ts_to_utc, utc_now_rfc3339_ms
from panopticon_py.utils.process_guard import update_heartbeat
from config import get_z_threshold, get_min_history_for_z

# Lazy MetricsCollector getter (avoids circular import)
def _mc():
    try:
        from panopticon_py.metrics import get_collector
        return get_collector()
    except Exception:
        return None


# D81: Lazy TE cache getter (avoids circular import)
def _te():
    try:
        from panopticon_py.signal.transfer_entropy_cache import get_te_cache
        return get_te_cache()
    except Exception:
        return None

logger = logging.getLogger(__name__)


def _utc() -> str:
    """Canonical UTC timestamp for persisted/internal contract fields."""
    return utc_now_rfc3339_ms()


# ── BTC 5m Dynamic Window Resolution (D70 Q1) ───────────────────────────────

ET_OFFSET_SECS = -18000  # UTC-5 (ET standard); adjust for EDT if needed

def current_5m_window_start_utc() -> int:
    """
    Return current 5-min window start in UTC Unix seconds.
    BTC 5m slugs use ET-aligned 5-minute buckets.
    Verified: slug = f"btc-updown-5m-{window_ts}"
    where window_ts = (now_et // 300) * 300 expressed as UTC seconds.
    """
    now_utc = int(time.time())
    now_et  = now_utc + ET_OFFSET_SECS
    ws_et   = (now_et // 300) * 300
    return ws_et - ET_OFFSET_SECS  # back to UTC


async def resolve_btc_5m_windows(db: ShadowDB, lookahead: int = 3) -> int:
    """
    D70 Q1: Resolve current + next `lookahead` BTC 5m windows into link_map.
    Called every 5 minutes from the resolve loop.
    Returns number of NEW rows inserted.

    Process per window slug:
      1. Skip if already in link_map (avoid redundant API calls)
      2. GET gamma-api.polymarket.com/markets?slug=<slug>
      3. Extract conditionId + clobTokenIds[0]
      4. INSERT OR IGNORE into polymarket_link_map
         (slug, condition_id, token_id, market_tier='t1', source='btc5m_resolver')

    RULE-API-1: clobTokenIds is returned as JSON STRING — must json.loads()
    """
    import json as _json

    GAMMA = "https://gamma-api.polymarket.com"
    now_utc   = int(time.time())
    now_et    = now_utc + ET_OFFSET_SECS
    ws_base   = (now_et // 300) * 300

    # ws_base is window start in ET seconds; slug needs UTC timestamp:
    # slug = f"btc-updown-5m-{ws_utc}" where ws_utc = ws_base - ET_OFFSET (convert ET->UTC)
    ws_utc_current = ws_base - ET_OFFSET_SECS

    slugs_to_try = [
        f"btc-updown-5m-{ws_utc_current - 300}",   # previous window (UTC)
    ]
    for i in range(lookahead + 1):
        slugs_to_try.append(f"btc-updown-5m-{ws_utc_current + i*300}")  # current + lookahead windows (UTC)

    inserted = 0
    for slug in slugs_to_try:
        existing = db.conn.execute(
            "SELECT 1 FROM polymarket_link_map WHERE slug=?", (slug,)
        ).fetchone()
        if existing:
            logger.debug("[LINK_MAP] %s already in link_map, skipping", slug)
            continue

        try:
            import urllib.request as _urllib
            params = urllib.parse.urlencode({"slug": slug})
            url = f"{GAMMA}/markets?{params}"
            req = _urllib.Request(url, headers={"User-Agent": "panopticon/1.0", "Accept": "application/json"})
            with _urllib.urlopen(req, timeout=5) as resp:
                markets = _json.loads(resp.read().decode("utf-8")) if resp.status == 200 else []
            if not markets:
                logger.debug("[LINK_MAP] %s not found yet", slug)
                continue
            m   = markets[0] if isinstance(markets, list) else markets
            cid = m.get("conditionId", "")
            ids = m.get("clobTokenIds") or "[]"
            if isinstance(ids, str):
                ids = _json.loads(ids)
            token_id = ids[0] if ids else ""
            if not cid or not token_id:
                logger.warning("[LINK_MAP] %s missing conditionId or token_id", slug)
                continue

            # fetched_at is NOT NULL — include it; use INSERT with ON CONFLICT
            db.conn.execute("""
                INSERT INTO polymarket_link_map
                    (slug, condition_id, token_id, market_tier, source, fetched_at, created_at)
                VALUES (?, ?, ?, 't1', 'btc5m_resolver', datetime('now'), datetime('now'))
                ON CONFLICT(market_id) DO UPDATE SET
                    slug = excluded.slug,
                    condition_id = excluded.condition_id,
                    token_id = excluded.token_id,
                    market_tier = excluded.market_tier,
                    source = excluded.source,
                    fetched_at = excluded.fetched_at
            """, (slug, cid, token_id))
            db.conn.commit()
            inserted += 1
            logger.info("[LINK_MAP] Resolved %s -> %s... token=%s...",
                        slug, cid[:12], token_id[:12])
        except Exception as e:
            logger.warning("[LINK_MAP] resolve error %s: %s", slug, e)

    return inserted


async def _btc5m_resolve_loop(db: ShadowDB) -> None:
    """
    D70 Q1: Background loop that resolves BTC 5m windows every 5 minutes.
    Runs as an independent asyncio task alongside _live_ticks.
    """
    import time as _time

    last_resolve = 0.0
    interval = 300.0  # 5 minutes

    while True:
        try:
            now = _time.monotonic()
            if now - last_resolve >= interval:
                last_resolve = now
                new_rows = await resolve_btc_5m_windows(db, lookahead=3)
                if new_rows:
                    total = db.conn.execute(
                        "SELECT COUNT(*) FROM polymarket_link_map"
                    ).fetchone()[0]
                    logger.info(
                        "[LINK_MAP] +%d new BTC 5m rows, total=%d",
                        new_rows, total,
                    )
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[BTC5M_RESOLVE_LOOP] error: %s", e)
            await asyncio.sleep(30)


# ── Dynamic subscription refresh ────────────────────────────────────────────

# ── MetricsCollector JSON loop (5s cadence) ───────────────────────────────────

async def _metrics_json_loop(
    mc,
    db,
    path: str = "data/rvf_live_snapshot.json",
) -> None:
    """
    Writes MetricsCollector JSON snapshot every 5s.
    Also syncs consensus stats from DB every 5s (no-op if error).
    D81: identity_coverage and TE stats synced every 60s.
    Periodically fetches event names for markets without them.
    Cancelled when parent task is cancelled.
    """
    import urllib.request, json as _json
    import urllib.parse

    last_event_fetch = 0.0
    fetch_interval = 3600  # 1 hour between batch fetches
    heartbeat_fixed_logged = False
    _loop_count = 0

    while True:
        try:
            await asyncio.sleep(5)
            _loop_count += 1
            update_heartbeat("radar")
            if not heartbeat_fixed_logged:
                logger.info("[D76_HEARTBEAT_FIXED] update_heartbeat resolved — metrics_json_loop stable")
                heartbeat_fixed_logged = True
            mc.sync_consensus_from_db(db)
            mc.persist_json(path=path)

            # D81: Sync coverage + TE stats every 60s (every 12 × 5s iterations)
            if _loop_count % 12 == 0:
                mc.sync_coverage_from_db(db)
                mc.sync_te_stats()

            # Background job: fetch event names for markets without them
            now = time.time()
            if now - last_event_fetch >= fetch_interval:
                last_event_fetch = now
                _fetch_missing_event_names(db)

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("[METRICS_JSON_LOOP][ERROR] %s", exc)


def _fetch_missing_event_names(db, batch_size: int = 20, lookback_days: int = 30) -> None:
    """
    D65 Q3: Batch-fetch event names from Gamma API for recent markets missing from link_map.

    Changes from original:
    - Uses batch API (up to 20 token_ids per call) instead of one-at-a-time
    - Joins on LOWER(token_id) = LOWER(market_id) (correct join key)
    - Only fetches markets in execution_records from last `lookback_days` days
      (historical markets outside window are skipped permanently — Case B1)
    - Only fills rows where source IS NULL or source='fallback'
    """
    import time as _time

    # D65 Q3: Join on LOWER(token_id) = LOWER(market_id), filter by lookback_days
    cutoff_ts = db.conn.execute(
        "SELECT datetime('now', ? || ' days')",
        (str(-lookback_days),)
    ).fetchone()[0]

    rows = db.conn.execute("""
        SELECT DISTINCT LOWER(er.market_id) as market_id_lower, er.market_id
        FROM execution_records er
        LEFT JOIN polymarket_link_map plm
            ON LOWER(plm.token_id) = LOWER(er.market_id)
        WHERE er.market_id IS NOT NULL
          AND er.market_id != ''
          AND er.created_ts_utc >= ?
          AND (plm.token_id IS NULL
               OR plm.source IS NULL
               OR plm.source = 'fallback'
               OR plm.event_slug IS NULL
               OR plm.event_slug = ''
               OR plm.event_slug LIKE '%...')
        LIMIT 200
    """, (cutoff_ts,)).fetchall()

    if not rows:
        return

    # D65 Q3: Deduplicate by lower-case market_id, preserve original case for DB
    seen, token_batch = set(), []
    for (market_id_lower, market_id) in rows:
        if market_id_lower not in seen:
            seen.add(market_id_lower)
            token_batch.append(market_id)

    logger.info("[EVENT_NAME_FETCH][D65] batch_size=%d lookback=%ddays fetched %d unique market_ids",
                batch_size, lookback_days, len(token_batch))

    filled = 0
    for i in range(0, len(token_batch), batch_size):
        batch = token_batch[i:i + batch_size]
        batch_filled = _gamma_batch_fetch_event_names(db, batch)
        filled += batch_filled
        _time.sleep(0.5)  # rate limit courtesy

    logger.info("[EVENT_NAME_FETCH][D65] total filled=%d", filled)


def _gamma_batch_fetch_event_names(db, token_ids: list[str]) -> int:
    """
    Fetch event metadata for a batch of token_ids using Gamma batch API.

    GET https://gamma-api.polymarket.com/markets?clob_token_ids=ID1,ID2,...

    D65 Q3 ruling: max 20 IDs per call (>20 returns HTTP 422).
    Returns number of rows successfully inserted into polymarket_link_map.
    """
    import json as _json

    joined = ",".join(str(t) for t in token_ids)
    params = urllib.parse.urlencode({"clob_token_ids": joined})
    url = f"https://gamma-api.polymarket.com/markets?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "panopticon/1.0", "Accept": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = _json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("[EVENT_NAME_FETCH][D65] batch fetch failed: %s", exc)
        return 0

    if not isinstance(markets, list):
        markets = [markets]

    inserted = 0
    for m in markets:
        try:
            # D65 Q3: extract token_id from clobTokenIds array
            clob_ids = m.get("clobTokenIds") or []
            if isinstance(clob_ids, str):
                try:
                    clob_ids = _json.loads(clob_ids)
                except Exception:
                    clob_ids = [clob_ids]
            if not isinstance(clob_ids, list):
                clob_ids = [clob_ids]

            slug = m.get("slug") or m.get("event_slug") or ""
            question = m.get("question") or m.get("title") or ""

            for tid in clob_ids:
                tid_str = str(tid)
                # Only insert if this token_id is in our batch
                if tid_str not in [str(t) for t in token_ids]:
                    continue
                if question and len(question) > 5:
                    db.conn.execute("""
                        INSERT OR IGNORE INTO polymarket_link_map
                            (token_id, event_slug, market_slug, canonical_event_url,
                             source, fetched_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """, (
                        tid_str,
                        question,
                        slug,
                        f"https://polymarket.com/markets/{slug}" if slug else "",
                        "batch_fetch",
                    ))
                    inserted += 1
        except Exception as exc:
            logger.warning("[EVENT_NAME_FETCH][D65] failed to parse market: %s", exc)

    if inserted:
        db.conn.commit()
        logger.info("[EVENT_NAME_FETCH][D65] inserted=%d from batch of %d", inserted, len(token_ids))
    return inserted


def _batch_fill_link_map(db_path: str, batch_size: int = 20, lookback_days: int = 30) -> int:
    """
    D65 Q3: Batch fill polymarket_link_map for recent markets only.

    Changes from D58b original:
    - Max 20 IDs per call (D65 Q3 ruling: >20 returns HTTP 422)
    - Uses LOWER(token_id) = LOWER(market_id) for correct join
    - lookback_days filter: only fetches markets from last N days
      (historical markets are NOT retried — Case B1 confirmed)
    - Only fills rows where source IS NULL or source='fallback'

    Call once at startup and optionally on-demand.
    """
    import sqlite3, urllib.parse, json as _json, time as _time

    conn = sqlite3.connect(db_path)
    cutoff_ts = conn.execute(
        "SELECT datetime('now', ? || ' days')",
        (str(-lookback_days),)
    ).fetchone()[0]

    rows = conn.execute("""
        SELECT DISTINCT LOWER(er.market_id) as mid_lower, er.market_id
        FROM execution_records er
        LEFT JOIN polymarket_link_map plm
            ON LOWER(plm.token_id) = LOWER(er.market_id)
        WHERE er.market_id IS NOT NULL
          AND er.market_id != ''
          AND er.created_ts_utc >= ?
          AND (plm.token_id IS NULL
               OR plm.source IS NULL
               OR plm.source = 'fallback')
    """, (cutoff_ts,)).fetchall()
    conn.close()

    # Deduplicate by lower-case market_id
    seen, token_ids = set(), []
    for (mid_lower, market_id) in rows:
        if mid_lower not in seen:
            seen.add(mid_lower)
            token_ids.append(market_id)

    if not token_ids:
        logger.info("[D65][BATCH_FILL] No recent token_ids to fill (lookback=%d days)", lookback_days)
        return 0

    logger.info("[D65][BATCH_FILL] Starting for %d token_ids (lookback=%d days)",
                len(token_ids), lookback_days)
    filled = 0

    for i in range(0, len(token_ids), batch_size):
        batch = token_ids[i:i + batch_size]
        filled += _gamma_batch_fill_link_map(db_path, batch)
        _time.sleep(0.5)

    logger.info("[D65][BATCH_FILL] Done — %d rows added", filled)
    return filled


def _gamma_batch_fill_link_map(db_path: str, token_ids: list[str]) -> int:
    """
    Fetch event metadata for a batch of token_ids and insert into polymarket_link_map.

    D65 Q3 ruling: max 20 IDs per call (>20 returns HTTP 422).
    Returns count of rows inserted.
    """
    import sqlite3, urllib.parse, json as _json

    joined = ",".join(str(t) for t in token_ids)
    params = urllib.parse.urlencode({"clob_token_ids": joined})
    url = f"https://gamma-api.polymarket.com/markets?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "panopticon/1.0", "Accept": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            markets = _json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.warning("[D65][BATCH_FILL] batch fetch failed: %s", e)
        return 0

    if not isinstance(markets, list):
        markets = [markets]

    conn = sqlite3.connect(db_path)
    inserted = 0
    for m in markets:
        try:
            clob_ids = m.get("clobTokenIds") or []
            if isinstance(clob_ids, str):
                try:
                    clob_ids = _json.loads(clob_ids)
                except Exception:
                    clob_ids = [clob_ids]
            if not isinstance(clob_ids, list):
                clob_ids = [clob_ids]

            slug = m.get("slug") or m.get("event_slug") or ""
            question = m.get("question") or m.get("title") or ""

            for tid in clob_ids:
                tid_str = str(tid)
                if tid_str not in [str(t) for t in token_ids]:
                    continue
                if question and len(question) > 5:
                    conn.execute("""
                        INSERT OR IGNORE INTO polymarket_link_map
                            (token_id, event_slug, market_slug, source, fetched_at)
                        VALUES (?, ?, ?, ?, datetime('now'))
                    """, (tid_str, question, slug, "batch_fetch"))
                    inserted += 1
        except Exception as exc:
            logger.warning("[D65][BATCH_FILL] failed to parse market: %s", exc)

    conn.commit()
    conn.close()
    logger.info("[D65][BATCH_FILL] batch inserted=%d of %d", inserted, len(token_ids))
    return inserted


# ── MetricsCollector startup baseline sync ────────────────────────────────────

def _sync_metrics_baseline(db, mc) -> None:
    """
    One-time startup: read DB counts → set MetricsCollector baseline.
    This ensures frontend shows accurate cumulative counts immediately.
    Does DB reads only at startup (not in hot path).
    """
    kyle_total = db.conn.execute(
        "SELECT COUNT(*) FROM kyle_lambda_samples WHERE window_ts > 0"
    ).fetchone()[0] or 0
    paper_total = db.conn.execute(
        "SELECT COUNT(*) FROM execution_records WHERE mode='PAPER'"
    ).fetchone()[0] or 0
    paper_wins = db.conn.execute(
        "SELECT COUNT(*) FROM execution_records WHERE mode='PAPER' AND accepted=1"
    ).fetchone()[0] or 0
    mono_viol = db.conn.execute(
        "SELECT COUNT(*) FROM series_violations WHERE violation_type='MONOTONE_VIOLATION'"
    ).fetchone()[0] or 0
    pre_cat = db.conn.execute(
        "SELECT COUNT(*) FROM series_violations WHERE violation_type='PRE_CATALYST_SIGNAL'"
    ).fetchone()[0] or 0
    smart_ex = db.conn.execute(
        "SELECT COUNT(*) FROM series_violations WHERE violation_type='SMART_EXIT'"
    ).fetchone()[0] or 0

    mc._paper_trades_total = paper_total
    mc._paper_win_count = paper_wins
    mc._paper_win_rate = paper_wins / paper_total if paper_total > 0 else 0.0
    mc._monotone_violations = mono_viol

    # ── MetricsCollector hook: Consensus / wallet stats baseline ─────────────────
    mc.sync_consensus_from_db(db)  # D48: populate L5 consensus stats immediately

    logger.info(
        "[METRICS_SYNC] baseline loaded: kyle=%d paper=%d/%d_wins mono=%d pre_cat=%d smart_ex=%d",
        kyle_total, paper_total, paper_wins, mono_viol, pre_cat, smart_ex,
    )


_last_subscription_refresh: float = 0.0
_last_tier1_refresh: float = 0.0
_last_pol_refresh: float = 0.0  # D101: T2-POL 30-min refresh cadence
_pol_active_count: int = 0  # D105: cached count for heartbeat visibility
_current_tokens: list[str] = []
_pending_reconnect: bool = False
_refresh_interval_sec: float = 60.0
_TIER1_REFRESH_INTERVAL_SEC = 60.0  # Tier 1 refreshes every 60s (5-min markets expire frequently)
_POL_REFRESH_INTERVAL_SEC = 1800.0  # D101: T2-POL refreshes every 30 minutes

# D121: WS subscription token limit (stay under 1MB Polymarket payload limit)
_WS_TOKEN_LIMIT = 200  # 200 tokens × ~66 bytes/addr ≈ 13KB, well under 1MB
# D121: Timestamp of last 1009 error — used to prevent immediate rebuild after FATAL payload error
_ws_1009_last_failure: float = 0.0

# D30: preserve last successful tier token sets to avoid subscription flapping
# when a refresh call is rate-limited and returns [].
_cached_t1_tokens: list[str] = []
_cached_t2_tokens: list[str] = []


# D137-2: Radar active market snapshot — written by _write_active_market_snapshot()
#          read by /api/radar/active-markets endpoint in app.py
def _write_active_market_snapshot(tier: str, token_ids: list[str], slugs: dict[str, str]) -> None:
    """
    D137-2: Write current WS subscription token list to data/radar_active_markets.json.
    Written after each tier refresh (T1: 60s, T5: 60s). Exceptions are always silent
    to保证主路徑不受影響。
    """
    try:
        snap_path = Path(os.getenv("RADAR_ACTIVE_MARKETS_PATH", "data/radar_active_markets.json"))
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if snap_path.exists():
            try:
                existing = json.loads(snap_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing[tier] = {
            "token_ids": token_ids,
            "slugs": slugs,
            "count": len(token_ids),
            "updated_ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%%03dZ"),
        }
        snap_path.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
_cached_t5_tokens: list[str] = []
_cached_t3_tokens: list[str] = []

# D121: Build WS subscription token list within payload size limit.
# Priority: T1 (BTC 5m) > POL T2 > general T2. T3/T5 excluded — REST polling sufficient.
# Extracts POL tokens directly from _token_tier_map (avoids threading pol_tokens through call chain).
def _build_ws_token_list(
    t1_tokens: list[str],
    t2_tokens: list[str],
    limit: int = _WS_TOKEN_LIMIT,
) -> list[str]:
    """
    D121: Build WS subscription token list within payload size limit.
    Priority: T1 > POL (T2-POL) > T2 general.
    T3/T5 excluded from WS entirely — REST polling sufficient for those tiers.
    POL tokens are derived from _token_tier_map at call time (global state, set by refresh loop).
    """
    ws_tokens: list[str] = []
    seen: set[str] = set()

    # Derive POL tokens from _token_tier_map at call time
    pol_tokens = [tok for tok, tier in _token_tier_map.items() if tier == "t2_pol"]

    # Priority 1: All T1 tokens (~30, includes BTC 5m)
    for tok in t1_tokens:
        if tok not in seen and len(ws_tokens) < limit:
            ws_tokens.append(tok)
            seen.add(tok)

    # Priority 2: POL tokens (~50-100, YES+NO)
    for tok in pol_tokens:
        if tok not in seen and len(ws_tokens) < limit:
            ws_tokens.append(tok)
            seen.add(tok)

    # Priority 3: T2 general (fill remaining budget)
    remaining = limit - len(ws_tokens)
    for tok in t2_tokens:
        if tok not in seen and remaining > 0:
            ws_tokens.append(tok)
            seen.add(tok)
            remaining -= 1

    logger.info(
        "[WS_BUILD] ws_tokens=%d (t1=%d pol=%d t2_fill=%d) limit=%d",
        len(ws_tokens),
        len([t for t in t1_tokens if t in seen]),
        len([t for t in pol_tokens if t in seen]),
        len(ws_tokens) - len([t for t in t1_tokens if t in seen]) - len([t for t in pol_tokens if t in seen]),
        limit,
    )
    return ws_tokens

# ── Module-level DB reference ──────────────────────────────────────────────────
# Set once in _main_async() before any async tasks that need DB access.
# Avoids threading db through every function signature in the call chain.
_radar_db: ShadowDB | None = None

# Token → market tier mapping (populated during subscription refresh)
# Used by SignalEvent.market_tier when entropy fires
_token_tier_map: dict[str, str] = {}  # token_id -> "t1"|"t2"|"t3"|"t5"

# Token → market endtime (Unix timestamp, for T5 time-decay weighting)
_token_endtime_map: dict[str, float] = {}  # token_id -> endDate Unix ts

# Token → slug mapping (for T1 window tracking + pruning)
_token_to_slug_map: dict[str, str] = {}  # token_id -> slug (e.g. "btc-updown-5m-1777018200")

# EntropyWindow buffers keyed by token_id (T1 rolling windows only)
# T2/T3/T5 markets use the single shared ew in _live_ticks; T1 markets
# get their own EntropyWindow per window to prevent cross-window contamination.
_entropy_windows: dict[str, EntropyWindow] = {}

# Raw T2 market dicts (for series detection) — populated in _refresh_tier2_tokens
_t2_raw_markets: list[dict] = []  # list of market dicts passing _is_tier2_market

# Tier 1: high-frequency 5-min up/down crypto markets (BTC, ETH, SOL, etc.)
# These expire every 5 minutes and have high trade frequency → ideal for Kyle λ calibration
_TIER1_END_WINDOW_MIN_SEC = 60      # at least 1 min before expiry
_TIER1_END_WINDOW_MAX_SEC = 2100     # at most 35 min before expiry
_TIER1_MIN_VOLUME_USD = 100.0       # minimum 24h volume to filter illiquid markets
_TIER1_SLUG_KEYWORDS = [
    "updown-5m", "up-or-down-5", "btc-updown", "bitcoin-updown",
    "btc-up", "eth-up",
    "sol-up", "xrp-up", "doge-up",
]

# Tier 2: short-duration event markets (3–30 days) — highest Smart Money edge
# Filters out algorithmic crypto markets (T1) and long-tail markets (T3/T4)
_TIER2_END_DAYS_MIN = 3      # at least 3 days from now
_TIER2_END_DAYS_MAX = 30     # at most 30 days from now
_TIER2_MIN_VOLUME_USD = 500.0   # minimum 24h volume (lowered from 5000 for shadow discovery)
_TIER2_SLUG_EXCLUDE_KEYWORDS = [
    "updown", "up-or-down", "5m", "15m", "1h", "hour",
    "minutes", "btc-up", "eth-up", "sol-up", "xrp-up", "doge-up",
]
_TIER2_CATEGORY_EXCLUDE = [
    "sports", "soccer", "basketball", "esports", "football",
    "tennis", "baseball", "mma", "boxing",
]

# Tier 5: LIVE sports markets — use conservative p_prior = 0.50 (no financial insider)
_TIER5_SPORTS_CATEGORIES = [
    "sports", "soccer", "basketball", "esports", "football",
    "tennis", "baseball", "mma", "boxing",
]
_TIER5_EXCLUDE_SEASON_KEYWORDS = [
    "champion", "winner", "champion-2026", "champion-2027",
    "world-cup-winner", "nba-champion", "nfl-champion",
    "superbowl", "stanley-cup",
]

# D113: Dual-strategy slug keywords for team-specific Gamma entries
# D116: Extended with most-common match format "-vs-" and team-specific patterns
# Must have trailing dash/prefix to avoid false matches (e.g. "nba-" not "nba")
_TIER5_SLUG_SPORTS_KEYWORDS = [
    # D113 original
    "nba-", "nfl-", "epl-", "premier-league-",
    "champions-league-", "fa-cup-", "bundesliga-",
    "will-win-the-match", "beats-", "wins-the-match",
    "match-winner", "score-more-goals",
    # D116 additions
    "-vs-",           # "arsenal-vs-man-city" (most common team match format)
    "to-win-",        # "arsenal-to-win-the-match"
    "total-goals-",   # total goals markets
    "over-under-",    # O/U markets
    "first-goal-",    # first goal markets
    "half-time-",     # half-time result markets
    "most-assists-",  # assists stat markets
    "top-scorer-",    # top scorer markets
    "handball-",      # handball leagues
    "hockey-",        # ice hockey
    "rugby-",         # rugby leagues
]

# D113: Guard against POL/crypto markets caught by slug-only match
_TIER5_SLUG_POL_GUARD = [
    "trump", "election", "tariff", "senate", "btc", "eth", "crypto",
    "will-trump", "president", "government",
    # D118: Preventive expansion — these names can appear in championship market slugs
    "harris", "biden", "desantis", "pelosi", "zuckerberg", "musk",
]

_TIER5_MAX_END_SEC = 3888000   # 45 days; D117: championship/semifinal markets settle in 1-6 weeks


def _t5_time_decay_weight(end_time_ts: float) -> float:
    """
    D117: Time-to-event decay weight for T5 scoring.
    Closer to event = stronger Smart Money signal.

    time_to_event:  < 6h   → weight 1.0  (live/in-play, strongest signal)
    time_to_event:  6–24h  → weight 0.85 (pre-game, near term)
    time_to_event:  1–3d   → weight 0.65 (pre-game, medium term)
    time_to_event:  3–7d   → weight 0.45 (pre-game, longer term)
    time_to_event:  > 7d   → weight 0.30 (speculative, championship markets)
    """
    now = time.time()
    tte_hours = max(0.0, (end_time_ts - now) / 3600.0)
    if tte_hours < 6:
        return 1.0
    elif tte_hours < 24:
        return 0.85
    elif tte_hours < 72:
        return 0.65
    elif tte_hours < 168:
        return 0.45
    else:
        return 0.30


def _is_tier1_market(m: dict) -> bool:
    """
    Return True if market m qualifies as a Tier 1 high-frequency 5-min market.

    Diagnostic findings (2026-04-24):
      - endDateIso can be "2026-07-31" (date only, no time) → naive datetime
      - endDate is "2026-07-31T12:00:00Z" (full ISO with Z)
      - volume24hr is a numeric float (not string)
      - slug patterns: "btc-updown-5m-{timestamp}", "btc-up-or-down-5", etc.
    """
    slug = str(m.get("slug") or "").lower()
    question = str(m.get("question") or "").lower()

    # Detect BTC/crypto 5-min up/down markets by slug AND question text
    is_btc_5m = (
        any(kw in slug for kw in _TIER1_SLUG_KEYWORDS)
        or (
            ("bitcoin" in slug or "btc" in slug)
            and ("up" in slug or "down" in slug)
        )
        or (
            "bitcoin" in question
            and ("5 minute" in question or "5-min" in question or "5m" in question)
        )
    )
    if not is_btc_5m:
        return False

    # Volume: try volume24hr first (numeric float in live API),
    # fall back to volumeNum (can be int or string)
    try:
        vol = float(m.get("volume24hr") or m.get("volumeNum") or m.get("volume") or 0)
        if vol < _TIER1_MIN_VOLUME_USD:
            return False
    except (ValueError, TypeError):
        pass

    # Expiry window: try endDate first (full ISO "2026-04-24T15:00:00Z"),
    # fall back to endDateIso ("2026-07-31" date-only, no timezone)
    end_raw = m.get("endDate") or m.get("endDateIso") or ""
    if not end_raw:
        return False
    try:
        end_str = str(end_raw)
        if "T" not in end_str:
            # Date-only format "2026-07-31" → treat as UTC midnight
            end_dt = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
        else:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        now_utc = datetime.now(timezone.utc)
        delta_sec = (end_dt - now_utc).total_seconds()
        if not (_TIER1_END_WINDOW_MIN_SEC <= delta_sec <= _TIER1_END_WINDOW_MAX_SEC):
            return False
    except (ValueError, TypeError):
        return False

    return True


# ── T1 EntropyWindow cleanup on window rollover ───────────────────────────────

def _cleanup_stale_entropy_windows(current_window_ts: int) -> None:
    """
    Remove EntropyWindow entries for T1 tokens from expired windows.
    Called every time T1 tokens are refreshed (60s interval).

    Logic:
      - Any asset_id in _entropy_windows whose slug maps to a window_ts
        that is < (current_window_ts - 300) is stale and should be removed.
      - This prevents unbounded memory growth and cross-window contamination.
      - T2/T3/T5 EntropyWindows are NOT affected — they are not keyed by T1
        rolling-window token_ids and are not stored in _entropy_windows.
    """
    stale_asset_ids = []
    stale_cutoff_ts = current_window_ts - 300  # one window back

    for asset_id, ew in list(_entropy_windows.items()):
        slug = _token_to_slug_map.get(asset_id, "")
        if not slug:
            continue
        # CONSTRAINT: Only clean up T1 tokens (rolling window pattern)
        # T1 pattern: "{asset}-updown-5m-{unix_ts}"
        if "updown-5m" not in slug:
            continue
        ts_part = slug.rsplit("-", 1)[-1]
        if not ts_part.isdigit():
            continue
        token_window_ts = int(ts_part)
        if token_window_ts < stale_cutoff_ts:
            stale_asset_ids.append(asset_id)
            logger.info(
                "[T1_EW_CLEANUP] removing stale EntropyWindow "
                "asset=%s window_ts=%d (current=%d, stale by %ds)",
                asset_id[:20], token_window_ts,
                current_window_ts,
                current_window_ts - token_window_ts,
            )

    for asset_id in stale_asset_ids:
        del _entropy_windows[asset_id]

    if stale_asset_ids:
        logger.info(
            "[T1_EW_CLEANUP] removed %d stale EntropyWindows, remaining: %d total",
            len(stale_asset_ids), len(_entropy_windows),
        )
        mc = _mc()
        if mc is not None:
            mc.on_entropy_window_cleanup(len(stale_asset_ids), len(_entropy_windows))


def _pending_trade_price(pending: dict | None) -> float:
    """Return pending trade price supporting legacy and current key names."""
    if not pending:
        return 0.0
    raw = pending.get("trade_price", pending.get("price", 0.0))
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _refresh_tier1_tokens(db) -> list[str]:
    """
    Refresh Tier 1 high-frequency 5-min up/down market tokens.

    Uses the T1 Market Clock (deterministic slug computation + direct Gamma lookup)
    instead of Gamma listing API. Slugs are computed from current Unix timestamp
    aligned to 5-min windows (btc-updown-5m-{ts}, eth-updown-5m-{ts}, sol-updown-5m-{ts}).
    This approach is not affected by Gamma API pagination or ordering issues.

    Refresh must run every 60s (or on T1 window boundary) since markets roll every 5 min.
    """
    global _last_tier1_refresh

    # ShadowDB instance for this thread (avoids threading issues with module globals)
    db = ShadowDB()
    # Use module-level _utc — no more threading issues after FIX-3

    now_monotonic = time.monotonic()
    if now_monotonic - _last_tier1_refresh < _TIER1_REFRESH_INTERVAL_SEC:
        return []  # rate-limit: skip if refreshed recently
    # D31 FIX: apply jitter to prevent alignment with rate-limit windows.
    # Random uniform jitter [0, 5] seconds prevents all T1 refresh cycles from
    # hitting the same Gamma API rate-limit bucket at the same wall-clock second.
    _last_tier1_refresh = now_monotonic - random.uniform(0, 5.0)
    _last_subscription_refresh = _last_tier1_refresh

    try:
        import asyncio as _asyncio
        from panopticon_py.hunting.t1_market_clock import refresh_t1_tokens_via_clock
    except ImportError as e:
        logger.warning("[RADAR] import failed for t1_market_clock: %s", e)
        return []

    try:
        # t1_market_clock is async; run in a fresh event loop (OK from sync thread)
        token_ids, _token_to_slug_from_clock = _asyncio.run(refresh_t1_tokens_via_clock())
    except Exception:
        logger.warning("[L1_TIER1_ZERO] Clock-based T1 resolution failed. Will retry on next cycle.")
        return []

    # ── Update _token_to_slug_map with fresh resolves + prune stale windows ─
    global _token_to_slug_map
    if _token_to_slug_from_clock:
        _token_to_slug_map.update(_token_to_slug_from_clock)
        # Prune tokens from windows older than 2 windows ago
        from panopticon_py.hunting.t1_market_clock import get_current_t1_window
        current_window = get_current_t1_window()
        stale_ts = current_window - 600  # 2 windows ago
        _token_to_slug_map = {
            tok: slug for tok, slug in _token_to_slug_map.items()
            if not slug.rsplit("-", 1)[-1].isdigit()
            or int(slug.rsplit("-", 1)[-1]) >= stale_ts
        }

        # ── Q9: Cleanup stale T1 EntropyWindows ─────────────────────────────────
        from panopticon_py.hunting.t1_market_clock import get_current_t1_window
        current_window = get_current_t1_window()
        _cleanup_stale_entropy_windows(current_window)

        # ── Q11: T1 Rolling Window Series Sync (60s interval) ───────────────────
        # Detect and update ROLLING_WINDOW series for T1 markets on every T1
        # refresh so the series_members table always reflects live subscriptions.
        from panopticon_py.series.series_detector import detect_series, SERIES_TYPE_ROLLING_WINDOW
        t1_market_dicts = [
            {
                "slug": slug,
                "conditionId": tok,
                # D30: detect_series path expects token_id in some code paths.
                # Keep both keys to avoid KeyError('token_id') during T1 sync.
                "token_id": tok,
                "market_tier": "t1",
            }
            for tok, slug in _token_to_slug_map.items()
            if "updown-5m" in slug
        ]
        if t1_market_dicts:
            t1_series = detect_series(t1_market_dicts)
            for series in t1_series:
                if series.series_type == SERIES_TYPE_ROLLING_WINDOW:
                    db.upsert_event_series({
                        "series_id": series.series_id,
                        "series_type": series.series_type,
                        "underlying_topic": series.underlying_topic or "",
                        "oracle_risk": series.oracle_risk or "UNKNOWN",
                        "notes": "",
                    })
                    for member in series.members:
                        db.upsert_series_member(series.series_id, {
                            "slug": getattr(member, "slug", member.token_id) or member.token_id,
                            "token_id": member.token_id,  # D30: db.upsert_series_member requires token_id
                            "conditionId": member.token_id,
                            "settlement_date": (
                                member.settlement_date.isoformat()
                                if getattr(member, "settlement_date", None) else ""
                            ),
                            "market_tier": "t1",
                        })
            logger.debug(
                "[T1_SERIES_SYNC] updated %d rolling window series",
                len(t1_series),
            )

            # D71e: Diagnostic — confirm BTC 5m condition_ids land in series_members
            try:
                _sm_t1_count = db.conn.execute(
                    "SELECT COUNT(*) FROM series_members WHERE market_tier='t1'"
                ).fetchone()
                logger.info(
                    "[DIAG][T1_SERIES_CHECK] series_detected=%d series_members_t1_count=%d",
                    len(t1_series), _sm_t1_count[0] if _sm_t1_count else 0,
                )
            except Exception as _e:
                logger.warning("[DIAG][T1_SERIES_CHECK] query failed: %s", _e)

    if not token_ids:
        logger.warning(
            "[L1_TIER1_ZERO] Clock-based T1 resolution returned 0 tokens. "
            "Gamma API may be delayed for current window."
        )
    else:
        logger.info(
            "[L1_TIER1] clock-based: %d T1 tokens resolved (%s...)",
            len(token_ids),
            token_ids[0][:20] if token_ids else "",
        )

    return token_ids


def _is_tier2_market(m: dict, now_utc: datetime) -> bool:
    """
    Return True if market m is a Tier 2 short-duration event market.

    Tier 2 = non-algorithmic, 3–30 day duration, ≥ $5K 24h volume.
    Highest Smart Money edge — insiders with early information have
    strongest advantage on short-duration geopolitical/科技 events.
    """
    slug = m.get("slug", "").lower()

    # Exclude resolved/closed markets (e.g. already-settled GPT release markets)
    if bool(m.get("resolved")) or bool(m.get("closed")):
        return False

    # Exclude algorithmic crypto up/down markets (T1)
    if any(kw in slug for kw in _TIER2_SLUG_EXCLUDE_KEYWORDS):
        return False

    # Exclude sports categories (T5) — check both category and groupItemTitle
    category = str(
        m.get("groupItemTitle") or
        m.get("category") or
        ""
    ).lower()
    if any(s in category for s in _TIER5_SPORTS_CATEGORIES):
        return False

    # Check 24h volume — try volume24hr first, fall back to volumeNum (Gamma quirk)
    try:
        vol = float(m.get("volume24hr") if m.get("volume24hr") is not None else m.get("volumeNum") or 0)
        if vol < _TIER2_MIN_VOLUME_USD:
            return False
    except (ValueError, TypeError):
        return False

    # Check expiry window: 3–30 days
    end_iso = m.get("endDateIso") or ""
    try:
        end_dt_raw = end_iso.replace("Z", "+00:00")
        if "+" not in end_dt_raw and "-" not in end_dt_raw[10:]:
            end_dt_raw += "+00:00"
        end_dt = datetime.fromisoformat(end_dt_raw)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta_days = (end_dt - now_utc).total_seconds() / 86400.0
        if not (_TIER2_END_DAYS_MIN <= delta_days <= _TIER2_END_DAYS_MAX):
            return False
    except (ValueError, TypeError):
        return False

    # Exclude near-certain markets (best bid near 0 or 1 has near-zero entropy alpha)
    # If bestBid is absent/missing, do NOT reject — Gamma omits it for some markets
    best_bid_raw = m.get("bestBid") or m.get("best_bid")
    if best_bid_raw is not None:
        try:
            best_bid = float(best_bid_raw)
            if best_bid >= 0.99 or best_bid <= 0.01:
                return False
        except (ValueError, TypeError):
            pass  # malformed bestBid → skip check, don't reject

    # ── E1: D22 Oracle Risk Classification ──────────────────────────────────
    # Tag oracle risk for downstream Kelly sizing. HIGH risk does NOT reject
    # the market — it just reduces position size (Invariant 5.2).
    # We tag via _oracle_risk field on market dict for series detection use.
    slug = m.get("slug", "").lower()
    oracle_risk = classify_oracle_risk(slug)
    if oracle_risk == ORACLE_RISK_HIGH:
        m["_oracle_risk"] = ORACLE_RISK_HIGH
        logger.debug(
            "[L1_TIER2][ORACLE_HIGH] slug=%s — subscribing with caution",
            slug[:40],
        )
    else:
        m["_oracle_risk"] = oracle_risk

    return True


def _detect_and_persist_series(markets: list[dict], db) -> None:
    """
    D21 Phase 1: Detect EventSeries from raw Gamma market list
    and persist to event_series + series_members + series_violations tables.

    This runs after every T2 refresh cycle (every 300s).
    Logs monotone violations (LOGGED only, no trade signal yet).
    """
    try:
        from panopticon_py.series.series_detector import detect_series
        from panopticon_py.series.monotone_checker import check_monotone_violations

        detected = detect_series(markets)
        deadline_count = sum(1 for s in detected if s.series_type == "DEADLINE_LADDER")
        rolling_count = sum(1 for s in detected if s.series_type == "ROLLING_WINDOW")

        for series in detected:
            # Persist event_series
            db.upsert_event_series({
                "series_id": series.series_id,
                "series_type": series.series_type,
                "underlying_topic": series.underlying_topic,
                "oracle_risk": series.oracle_risk,
                "created_ts_utc": _utc(),
            })
            # Persist each member
            for member in series.members:
                m_dict = {
                    "token_id": member.token_id,
                    "slug": member.slug,
                    "settlement_date": (
                        member.settlement_date.isoformat()
                        if member.settlement_date else ""
                    ),
                    "market_tier": member.market_tier,
                    "current_prob": member.current_prob,
                }
                db.upsert_series_member(series.series_id, m_dict)

            # Check monotone violations (DEADLINE_LADDER only)
            if series.series_type == "DEADLINE_LADDER":
                violations = check_monotone_violations(series)
                for v in violations:
                    db.write_series_violation(
                        series_id=v.series_id,
                        violation_type="MONOTONE_VIOLATION",
                        earlier_slug=v.earlier_slug,
                        later_slug=v.later_slug,
                        gap_pct=v.gap_pct,
                        action_taken="LOGGED",
                    )

        logger.info(
            "[L1_SERIES] detected=%d deadline_ladders=%d rolling=%d",
            len(detected), deadline_count, rolling_count,
        )

        # ── MetricsCollector hook: Series stats ─────────────────────────────────
        mc = _mc()
        if mc is not None:
            from panopticon_py.series.event_series import ORACLE_RISK_HIGH
            oracle_high = sum(
                1 for m in markets
                if m.get("_oracle_risk") == ORACLE_RISK_HIGH
            )
            mc.on_series_update(
                deadline_ladders=deadline_count,
                rolling_windows=rolling_count,
                monotone_violations=sum(
                    1 for s in detected
                    if s.series_type == "DEADLINE_LADDER"
                    and check_monotone_violations(s)
                ),
            )
            mc.on_oracle_high_risk(oracle_high)
    except Exception as e:
        import traceback as _tb
        logger.warning("[L1_SERIES] series detection failed: %s\n%s", e, _tb.format_exc())


async def _backward_lookback(
    market_id: str,
    token_id: str,
    catalyst_ts: float,
    prob_before: float,
    lookback_hours: int = 24,
    min_insider_score: float = 0.55,
    max_entry_prob: float = 0.25,
    min_trade_count: int = 2,
) -> None:
    """
    D21 Phase 2 — Pre-Catalyst Quiet Accumulation Detector.

    After a catalyst shock fires (entropy z-score threshold hit on T2 market),
    look backward N hours to find wallets that were quietly accumulating YES
    in a low-probability market before the shock.

    This is async fire-and-forget — does NOT block the WS event path.
    Results are logged to series_violations (PRE_CATALYST_SIGNAL type).
    No trade signal is generated in Phase 2.
    """
    lookback_start_ts = catalyst_ts - (lookback_hours * 3600)
    lookback_start_iso = datetime.fromtimestamp(
        lookback_start_ts, tz=timezone.utc
    ).isoformat()
    catalyst_ts_iso = datetime.fromtimestamp(
        catalyst_ts, tz=timezone.utc
    ).isoformat()

    # Skip if market was not low-prob before catalyst (not a meaningful pre-catalyst case)
    if prob_before > max_entry_prob:
        logger.debug(
            "[LOOKBACK][SKIP] market=%s prob_before=%.2f > threshold=%.2f — "
            "not a low-prob market, catalyst may be noise",
            market_id[:20], prob_before, max_entry_prob,
        )
        return

    try:
        pre_positioned = db.query_pre_catalyst_wallets(
            market_id=market_id,
            start_ts=lookback_start_iso,
            end_ts=catalyst_ts_iso,
            side="YES",
            min_trade_count=min_trade_count,
            max_entry_prob=max_entry_prob,
        )
    except Exception as e:
        logger.warning("[LOOKBACK][ERROR] market=%s: %s", market_id[:20], e)
        return

    if not pre_positioned:
        logger.info(
            "[LOOKBACK][EMPTY] market=%s no pre-catalyst wallets in last %dh (prob_before=%.2f)",
            market_id[:20], lookback_hours, prob_before,
        )
        return

    for wallet_row in pre_positioned:
        wallet_addr = wallet_row["wallet_address"]
        trade_count = wallet_row["trade_count"]
        avg_entry = wallet_row["avg_entry_prob"]
        total_size = wallet_row["total_size"]

        insider_score = db.get_latest_insider_score(wallet_addr)

        logger.info(
            "[LOOKBACK][PRE_CATALYST] market=%s wallet=%s...%s "
            "trade_count=%d avg_entry=%.3f total_size=%.2f insider_score=%.3f",
            market_id[:20],
            wallet_addr[:8], wallet_addr[-4:],
            trade_count, avg_entry, total_size,
            insider_score or 0.0,
        )

        # Write violation record (LOGGED only — no trade signal yet)
        series_id = db.get_series_id_for_market(market_id)
        db.write_series_violation(
            series_id=series_id,
            violation_type="PRE_CATALYST_SIGNAL",
            earlier_slug=None,
            later_slug=None,
            gap_pct=None,
            wallet_address=wallet_addr,
            action_taken="LOGGED",
        )

        # Upgrade insider_score observationally (does NOT trigger trade)
        if insider_score is not None and insider_score < min_insider_score:
            new_score = min(insider_score + 0.08, 0.85)
            try:
                db.update_insider_score(wallet_addr, new_score, "PRE_CATALYST_ACCUMULATION_DETECTED")
                logger.info(
                    "[LOOKBACK][SCORE_UPGRADE] wallet=%s...%s %.3f → %.3f",
                    wallet_addr[:8], wallet_addr[-4:],
                    insider_score, new_score,
                )
            except Exception:
                pass  # non-critical

    # Mark lookback done in catalyst_events
    try:
        db.mark_lookback_done(
            market_id=market_id,
            catalyst_ts_iso=catalyst_ts_iso,
            wallets_found=len(pre_positioned),
        )
    except Exception:
        pass  # non-critical


def _is_tier5_sports_market(m: dict) -> bool:
    """
    Return True if market m is a LIVE sports market (T5).

    D113: Dual-strategy detection:
      Strategy 1: groupItemTitle / category keyword match (generic sports words)
      Strategy 2: slug-level sports league/match keywords (team-specific Gamma entries)

    Must pass at least one strategy; slug-only match requires additional POL guard.
    """
    slug = str(m.get("slug") or m.get("conditionId") or "").lower()

    # Exclude long-horizon season/championship markets (NBA/FIFA/etc.)
    if any(kw in slug for kw in _TIER5_EXCLUDE_SEASON_KEYWORDS):
        return False

    # Strategy 1: groupItemTitle / category keyword match (generic sports words)
    category_raw = str(
        m.get("groupItemTitle") or
        m.get("category") or
        ""
    ).lower()
    category_match = any(s in category_raw for s in _TIER5_SPORTS_CATEGORIES)

    # Strategy 2: slug-level sports league/match keywords
    slug_match = any(kw in slug for kw in _TIER5_SLUG_SPORTS_KEYWORDS)

    # Must pass at least one strategy
    if not (category_match or slug_match):
        logger.debug(
            "[T5_FILTER] rejected slug=%-40s groupItemTitle=%s category=%s",
            slug[:40],
            m.get("groupItemTitle", "")[:30],
            m.get("category", "")[:20],
        )
        return False

    # Guard against POL/crypto markets caught by slug-only match
    if slug_match and not category_match:
        if any(g in slug for g in _TIER5_SLUG_POL_GUARD):
            # Pol-guard blocked — return False so else-branch counts it
            return False
        # slug_only passed — continue to active/expiry checks

    # T5 is only for short-horizon LIVE sports markets (<= 48h to settlement)
    end_iso = m.get("endDateIso") or ""
    if end_iso:
        try:
            end_dt_raw = end_iso.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(end_dt_raw)
            # fromisoformat may return naive or offset-naive-aware datetime.
            # If tzinfo is None → naive; assign UTC.
            # If offset-aware → subtract offset to get naive UTC, then assign UTC.
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            else:
                offset = end_dt.utcoffset()
                if offset is not None:
                    end_dt_naive_utc = (end_dt.replace(tzinfo=None) - offset)
                    end_dt = end_dt_naive_utc.replace(tzinfo=timezone.utc)
            delta_sec = (end_dt - datetime.now(timezone.utc)).total_seconds()
            if delta_sec > _TIER5_MAX_END_SEC:
                return False
        except (ValueError, TypeError):
            return False

    return True


def _refresh_tier2_tokens(db) -> list[str]:
    """
    Refresh Tier 2 short-duration event market tokens.

    These markets (3-30 day expiry, >= $500 volume) carry the highest Smart Money
    edge. Insiders with early information on geopolitical events have the
    strongest Bayesian update advantage. Refresh every 300s.

    Note: Gamma's end_date_min/max params are ignored; local endDate filtering
    is applied instead. Volume floor lowered to $500 for shadow discovery.
    """
    global _last_subscription_refresh

    # Use the passed-in db (from _refresh_all_subscriptions → asyncio.to_thread caller)

    try:
        import httpx
    except ImportError:
        logger.warning("[RADAR] httpx not installed, skipping Tier 2 market refresh")
        return []

    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        # Gamma ignores closed/endDate params; local filtering applied in _is_tier2_market
        url = f"{base}{path}?closed=false&limit=500"
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        markets = resp.json()
    except Exception:
        logger.warning("[RADAR] Failed to refresh Tier 2 markets via Gamma")
        return []

    now_utc = datetime.now(timezone.utc)
    tier2_tokens: list[str] = []
    seen: set[str] = set()
    tier2_count = 0
    raw_markets_count = 0
    for m in markets:
        if not isinstance(m, dict):
            continue
        raw_markets_count += 1
        if _is_tier2_market(m, now_utc):
            tier2_count += 1
            raw_tids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(raw_tids, str):
                try:
                    raw_tids = json.loads(raw_tids)
                except json.JSONDecodeError:
                    logger.warning("[TIER] clobTokenIds json.loads failed: %r", raw_tids[:80])
                    raw_tids = []
            elif not isinstance(raw_tids, list):
                raw_tids = [raw_tids]
            for tid in raw_tids:
                key = str(tid).strip() if tid is not None else ""
                if not key or key in seen:
                    continue
                seen.add(key)
                tier2_tokens.append(key)

    # ── D21: Event Series Detection ──────────────────────────────────────────
    # Run after T2 market list is built; persists series to DB + logs monotone violations
    # Cache T2-filtered market dicts for whale_scanner (Phase 5 D28)
    global _t2_raw_markets
    _t2_raw_markets = [m for m in markets if _is_tier2_market(m, now_utc)]
    if tier2_count > 0:
        _detect_and_persist_series(markets, db)

    if tier2_count > 0:
        logger.info(
            "[L1_TIER2] Refreshed %d Tier 2 event markets (%d tokens) from Gamma API",
            tier2_count,
            len(tier2_tokens),
        )
    elif not tier2_tokens:
        logger.warning(
            "[L1_TIER2_ZERO] _refresh_tier2_tokens() returned 0 tokens. "
            "Gamma API returned %d raw markets.",
            raw_markets_count,
        )
    return tier2_tokens


def _refresh_tier5_sports_tokens(db) -> list[str]:
    """
    Refresh Tier 5 LIVE sports market tokens.

    Sports markets require special handling: no financial insider signal,
    so signal_engine uses p_prior = 0.50 (conservative 50/50 base rate).
    Refresh every 60s since live sports markets expire within hours.
    """
    global _last_subscription_refresh

    try:
        import httpx
    except ImportError:
        logger.warning("[RADAR] httpx not installed, skipping Tier 5 sports refresh")
        return []

    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        url = f"{base}{path}?closed=false&limit=500"
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        markets = resp.json()
    except Exception:
        logger.warning("[RADAR] Failed to refresh Tier 5 sports markets via Gamma")
        return []

    tier5_tokens: list[str] = []
    seen: set[str] = set()
    tier5_count = 0
    raw_markets_count = 0
    # D117: Per-reason counters for detailed diagnostic
    diag: dict[str, int] = {
        "category_only": 0,
        "slug_only": 0,
        "pol_guard": 0,
        "season_kw": 0,
        "inactive": 0,
        "expiry>45d": 0,
        "bad_expiry": 0,
    }
    pol_guard_examples: list[str] = []
    slug_only_examples: list[str] = []
    for m in markets:
        if not isinstance(m, dict):
            continue
        raw_markets_count += 1
        slug_lc = str(m.get("slug") or "").lower()
        if _is_tier5_sports_market(m):
            tier5_count += 1
            raw_tids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            if isinstance(raw_tids, str):
                try:
                    raw_tids = json.loads(raw_tids)
                except json.JSONDecodeError:
                    logger.warning("[TIER] clobTokenIds json.loads failed: %r", raw_tids[:80])
                    raw_tids = []
            elif not isinstance(raw_tids, list):
                raw_tids = [raw_tids]
            # D117: cache endtime for T5 time-decay weighting
            end_ts = 0.0
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
                    end_ts = end_dt.timestamp()
                except (ValueError, TypeError):
                    pass
            for tid in raw_tids:
                key = str(tid).strip() if tid is not None else ""
                if not key or key in seen:
                    continue
                seen.add(key)
                tier5_tokens.append(key)
                _token_endtime_map[key] = end_ts
        else:
            # D117: Detailed rejection reason tracking
            category_lc = str(m.get("groupItemTitle") or m.get("category") or "").lower()
            category_match = any(s in category_lc for s in _TIER5_SPORTS_CATEGORIES)
            slug_match = any(kw in slug_lc for kw in _TIER5_SLUG_SPORTS_KEYWORDS)
            active = bool(m.get("active"))

            if any(kw in slug_lc for kw in _TIER5_EXCLUDE_SEASON_KEYWORDS):
                diag["season_kw"] += 1
            elif slug_match and not category_match:
                if any(g in slug_lc for g in _TIER5_SLUG_POL_GUARD):
                    diag["pol_guard"] += 1
                    if len(pol_guard_examples) < 3:
                        pol_guard_examples.append(slug_lc[:60])
                else:
                    diag["slug_only"] += 1
                    if len(slug_only_examples) < 3:
                        slug_only_examples.append(slug_lc[:60])
            elif category_match:
                # Category matched but rejected by another filter
                diag["category_only"] += 1

            if not active:
                diag["inactive"] += 1
            if active:
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
                            diag["expiry>45d"] += 1
                    except (ValueError, TypeError):
                        diag["bad_expiry"] += 1

    logger.info(
        "[L1_TIER5_DIAG] raw=%d tier5=%d | cat=%d slug=%d pol=%d season=%d inactive=%d expiry=%d bad=%d",
        raw_markets_count, tier5_count,
        diag["category_only"], diag["slug_only"], diag["pol_guard"],
        diag["season_kw"], diag["inactive"], diag["expiry>45d"], diag["bad_expiry"],
    )
    if pol_guard_examples:
        logger.info("[L1_TIER5_DIAG] pol_guard examples: %s", pol_guard_examples)
    if slug_only_examples:
        logger.info("[L1_TIER5_DIAG] slug_only examples: %s", slug_only_examples)

    if tier5_count > 0:
        logger.info(
            "[L1_TIER5] Refreshed %d Tier 5 sports markets (%d tokens) from Gamma API",
            tier5_count,
            len(tier5_tokens),
        )
    elif not tier5_tokens:
        logger.warning(
            "[L1_TIER5_ZERO] _refresh_tier5_sports_tokens() returned 0 tokens. "
            "Gamma API returned %d raw markets.",
            raw_markets_count,
        )
    return tier5_tokens


def _sync_pol_tokens_from_watchlist(db) -> list[str]:
    """
    D101: Sync T2-POL tokens from pol_market_watchlist into _token_tier_map.
    D111: Registers BOTH YES and NO tokens (token_id + token_id_no) as t2_pol.

    First performs a Gamma API scan (via sync httpx in thread), then reads
    the watchlist and registers t2_pol tokens in the shared token tier map.

    Returns list of all registered t2_pol token_ids (YES + NO combined).
    Must be called from a thread context (asyncio.to_thread).
    """
    global _token_tier_map, _pol_active_count

    # D105-3: POL scan elapsed time (measured inside thread)
    import time as _time
    _t0 = _time.monotonic()

    # Step 1: Scan Gamma API for new political markets
    from panopticon_py.hunting.pol_monitor import sync_scan_pol_markets
    try:
        scanned = sync_scan_pol_markets(db, max_pages=5)
        _elapsed = _time.monotonic() - _t0
        logger.info(
            "[POL_REFRESH] scan_complete count=%d elapsed=%.2fs next_refresh_in=%ds",
            scanned,
            _elapsed,
            _POL_REFRESH_INTERVAL_SEC,
        )
    except Exception as exc:
        _elapsed = _time.monotonic() - _t0
        logger.warning(
            "[POL_REFRESH] scan_failed elapsed=%.2fs error=%s",
            _elapsed,
            exc,
        )
        # D106: continue — do not return early; preserve last known POL tokens

    # Step 2: Read active political markets and register tokens
    pol_markets = db.fetch_active_pol_markets()
    _pol_active_count = len(pol_markets)
    pol_token_ids: list[str] = []

    # D103: Diagnostic — warn if watchlist is empty
    if not pol_markets:
        logger.warning(
            "[POL_WATCHLIST] no active political markets in watchlist. "
            "Either pol_monitor has not run yet (first boot) or "
            "Gamma API returned no matching markets. Next scan in %ds.",
            _POL_REFRESH_INTERVAL_SEC,
        )
    else:
        # D103: Per-market diagnostic log
        for m in pol_markets:
            slug = (m.get("event_slug") or m.get("market_id", "?"))[:40]
            keywords = ", ".join(m.get("entity_keywords") or [])
            last_sig = m.get("last_signal_ts") or "none"
            logger.info(
                "[POL_WATCHLIST] monitoring: %-40s  cat=%-15s  kw=[%s]  last_signal=%s",
                slug, m["political_category"], keywords, last_sig,
            )
        logger.info("[POL_WATCHLIST] %d active political markets loaded.", len(pol_markets))

    for pm in pol_markets:
        # D111: register both YES and NO tokens
        for tid in (pm.get("token_id"), pm.get("token_id_no")):
            if not tid:
                continue
            _token_tier_map[tid] = "t2_pol"
            pol_token_ids.append(tid)

    logger.info("[POL] registered %d t2_pol tokens from watchlist", len(pol_token_ids))
    return pol_token_ids


def _log_t5_market_status(db) -> None:
    """
    D103: Log T5 sports markets currently being monitored.
    Called on radar startup and after each POL refresh cycle.
    """
    t5_markets = db.fetch_active_t5_markets(lookback_hours=48)
    if not t5_markets:
        logger.warning(
            "[T5_WATCHLIST] no T5 sports markets seen in last 48h. "
            "Either radar has not processed any T5-tier signals yet, "
            "or sports markets subscription is not configured."
        )
        return
    for m in t5_markets:
        slug = m.get("market_id", "?")[:40]
        signals = int(m.get("total_signals") or 0)
        accepted = int(m.get("accepted") or 0)
        last_ts = m.get("last_signal_ts") or "?"
        logger.info(
            "[T5_WATCHLIST] monitoring: %-40s  signals=%-4d  accepted=%-4d  last=%s",
            slug, signals, accepted, last_ts,
        )
    logger.info("[T5_WATCHLIST] %d T5 sports markets active.", len(t5_markets))


def _refresh_active_subscription(db) -> list[str]:
    """
    每 _refresh_interval_sec 秒自動抓取活躍市場 clobTokenIds。
    Rate Limit 安全：Gamma API /markets?closed=false 限額 300 req/10s，
    每 1 分鐘呼叫 1 次 = 每小時 60 次，遠低於限額。
    """
    global _last_subscription_refresh, _refresh_interval_sec

    refresh_interval = float(os.getenv("WS_SUBSCRIBE_REFRESH_SEC", "60.0"))
    if refresh_interval != _refresh_interval_sec:
        _refresh_interval_sec = refresh_interval

    now = time.monotonic()
    if now - _last_subscription_refresh < _refresh_interval_sec:
        return []
    _last_subscription_refresh = now

    try:
        import httpx
    except ImportError:
        logger.warning("[RADAR] httpx not installed, skipping market refresh")
        return []

    try:
        base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").strip()
        path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets").strip()
        url = f"{base}{path}?closed=false&limit=100"
        resp = httpx.get(url, timeout=15.0)
        resp.raise_for_status()
        markets = resp.json()
        active_tokens: list[str] = []
        seen: set[str] = set()
        for m in markets:
            if not m.get("active") or m.get("closed"):
                continue
            raw_tids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
            # clobTokenIds can be a JSON string, a list, or a single value
            if isinstance(raw_tids, str):
                try:
                    raw_tids = json.loads(raw_tids)
                except json.JSONDecodeError:
                    raw_tids = []
            if not isinstance(raw_tids, (list, tuple)):
                raw_tids = [raw_tids]
            for tid in raw_tids:
                key = str(tid).strip() if tid is not None else ""
                if not key or key in seen:
                    continue
                seen.add(key)
                active_tokens.append(key)
        logger.info("[RADAR] Refreshed %d active tokens from Gamma API", len(active_tokens))
        return active_tokens
    except Exception:
        logger.warning("[RADAR] Failed to refresh active markets via Gamma, keeping current subscription")
        return []


async def _refresh_all_subscriptions(db) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    """
    Concurrently refresh all market tiers and return merged deduplicated token list
    plus individual tier token lists for logging.

    Uses asyncio.gather for concurrent Gamma API calls (one per tier),
    then merges into a single token list. _token_tier_map is populated
    for use in SignalEvent.market_tier.

    Refresh intervals (enforced per function):
      _refresh_tier1_tokens: 60s
      _refresh_tier2_tokens: 300s
      _refresh_tier5_sports_tokens: 60s
      _refresh_active_subscription: 60s
    """
    global _cached_t1_tokens, _cached_t2_tokens, _cached_t5_tokens, _cached_t3_tokens

    # Run all tier refreshers concurrently
    results = await asyncio.gather(
        asyncio.to_thread(_refresh_tier1_tokens, db),
        asyncio.to_thread(_refresh_tier2_tokens, db),
        asyncio.to_thread(_refresh_tier5_sports_tokens, db),
        asyncio.to_thread(_refresh_active_subscription, db),
        return_exceptions=True,
    )
    tier1_tokens, tier2_tokens, tier5_tokens, tier3_tokens = results

    # Handle any exceptions — treat failed refreshes as empty list
    if isinstance(tier1_tokens, Exception):
        logger.warning("[L1_SUBSCRIPTION] T1 refresh failed: %s", tier1_tokens)
        tier1_tokens = []
    if isinstance(tier2_tokens, Exception):
        logger.warning("[L1_SUBSCRIPTION] T2 refresh failed: %s", tier2_tokens)
        tier2_tokens = []
    if isinstance(tier5_tokens, Exception):
        logger.warning("[L1_SUBSCRIPTION] T5 refresh failed: %s", tier5_tokens)
        tier5_tokens = []
    if isinstance(tier3_tokens, Exception):
        logger.warning("[L1_SUBSCRIPTION] T3 refresh failed: %s", tier3_tokens)
        tier3_tokens = []

    # D30: if a refresh call returns [] (e.g., rate-limited), keep last good set.
    if tier1_tokens:
        _cached_t1_tokens = list(tier1_tokens)
    elif _cached_t1_tokens:
        tier1_tokens = list(_cached_t1_tokens)

    if tier2_tokens:
        _cached_t2_tokens = list(tier2_tokens)
    elif _cached_t2_tokens:
        tier2_tokens = list(_cached_t2_tokens)

    if tier5_tokens:
        _cached_t5_tokens = list(tier5_tokens)
    elif _cached_t5_tokens:
        tier5_tokens = list(_cached_t5_tokens)

    if tier3_tokens:
        _cached_t3_tokens = list(tier3_tokens)
    elif _cached_t3_tokens:
        tier3_tokens = list(_cached_t3_tokens)

    # Populate token → tier map
    for t in tier1_tokens:
        _token_tier_map[t] = "t1"
    for t in tier2_tokens:
        _token_tier_map[t] = "t2"
    for t in tier5_tokens:
        _token_tier_map[t] = "t5"
    for t in tier3_tokens:
        if t not in _token_tier_map:  # T3 is default/fallback; don't overwrite existing tiers
            _token_tier_map[t] = "t3"

    # Merge + deduplicate, preserving order (t1 → t2 → t5 → t3 priority)
    seen: set[str] = set()
    combined: list[str] = []
    for t in list(tier1_tokens) + list(tier2_tokens) + list(tier5_tokens) + list(tier3_tokens):
        if t not in seen:
            seen.add(t)
            combined.append(t)

    logger.info(
        "[L1_SUBSCRIPTION] total=%d (t1=%d t2=%d t5=%d t3=%d)",
        len(combined), len(tier1_tokens), len(tier2_tokens),
        len(tier5_tokens), len(tier3_tokens),
    )
    # D137-2: Write market snapshot for observability (silent — never affects main path)
    _write_active_market_snapshot("t1", list(tier1_tokens), dict(_token_to_slug_map))
    _write_active_market_snapshot("t5", list(tier5_tokens), {})
    # ── MetricsCollector hook (in-process, no DB reads) ─────────────────
    mc = _mc()
    if mc is not None:
        mc.on_l1_subscription(t1=len(tier1_tokens), t2=len(tier2_tokens),
                              t3=len(tier3_tokens), t5=len(tier5_tokens))
        # ── L1 Window: active EntropyWindow count ─────────────────────
        mc.on_entropy_window_active(len(_entropy_windows))

    return combined, tier1_tokens, tier2_tokens, tier5_tokens, tier3_tokens


async def _synthetic_ticks(ew: EntropyWindow, db: ShadowDB, duration_sec: float) -> None:
    """Deterministic-enough demo feed when ``--synthetic`` is set."""
    t0 = time.monotonic()
    buf: list[dict] = []
    next_heartbeat = t0 + 10.0
    while time.monotonic() - t0 < duration_sec:
        recv = time.monotonic()
        buy = random.uniform(0.5, 2.0)
        sell = random.uniform(0.5, 2.0)
        flushed = ew.push(recv, buy, sell)
        if flushed:
            pass
        ew.record_H_sample(recv)
        d, z = ew.zscore_of_latest_delta()
        addr = "0x%040x" % (abs(hash((recv, buy, sell))) % (16**40))
        buf.append(
            {
                "side": "BUY" if buy > sell else "SELL",
                "size": abs(buy - sell),
                "timestamp": recv * 1000,
                "taker": addr[:42],
            }
        )
        if z is not None and ew.should_fire_negative_entropy(get_z_threshold()):
            parents, virtuals = cross_wallet_burst_cluster(buf[-20:])
            _ = aggregate_taker_sweeps(buf[-20:])
            db.append_hunting_shadow_hit(
                {
                    "hit_id": str(uuid4()),
                    "address": "synthetic",
                    "market_id": None,
                    "entity_score": 0.0,
                    "entropy_z": float(z),
                    "sim_pnl_proxy": float(d or 0.0),
                    "outcome": None,
                    "payload_json": {"virtual_entities": len(virtuals), "parents": len(parents)},
                    "created_ts_utc": _utc(),
                }
            )
        now = time.monotonic()
        if now >= next_heartbeat:
            logger.info(
                "[RADAR HEARTBEAT] Buffer Events: %s, Trigger Locked: %s, Current Entropy H: %s",
                len(buf),
                bool(getattr(ew, "_lock_active", False)),
                getattr(ew, "last_H", None),
            )
            next_heartbeat = now + 10.0
        await asyncio.sleep(0.08)


# ── Data API polling ─────────────────────────────────────────────────────────

_last_data_api_poll: float = 0.0


def _poll_data_api_for_takers(token_ids: list[str], db: ShadowDB) -> None:
    """
    Poll Data API /trades for given token_ids to capture real taker addresses.
    Writes wallet_observations with obs_type='clob_trade' and real proxyWallet.
    """
    global _last_data_api_poll
    now = time.monotonic()

    poll_interval = float(os.getenv("DATA_API_POLL_INTERVAL_SEC", "30.0"))
    if now - _last_data_api_poll < poll_interval:
        return
    _last_data_api_poll = now

    if not token_ids:
        return

    data_api_base = "https://data-api.polymarket.com"
    seen = 0
    # Poll top 20 tokens (respects rate limit: 200 req/10s)
    for token_id in token_ids[:20]:
        try:
            import urllib.request
            url = f"{data_api_base}/trades?asset_id={token_id}&limit=10"
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "panopticon-radar/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                if resp.status >= 400:
                    continue
                raw = resp.read().decode("utf-8")
                trades = json.loads(raw)
        except Exception:
            continue

        if not isinstance(trades, list):
            continue

        for trade in trades:
            taker = str(trade.get("proxyWallet") or trade.get("taker_address") or "").strip()
            if not (taker.startswith("0x") and len(taker) >= 42):
                continue
            taker_addr = taker[:42].lower()
            order_id = try_match_or_open(trade, db)
            obs = {
                "obs_id": str(uuid4()),
                "address": taker_addr,
                "market_id": token_id,
                "obs_type": "clob_trade",
                "payload_json": {
                    "side": str(trade.get("side") or "BUY"),
                    "size": float(trade.get("size") or 0),
                    "price": float(trade.get("price") or 0),
                    "source": "data_api_polling",
                },
                "ingest_ts_utc": _utc(),
                "transaction_hash": trade.get("transactionHash", ""),
                "order_id": order_id,
            }
            db.append_wallet_observation(obs)
            seen += 1

    if seen > 0:
        logger.info("[RADAR][DATA_API] Captured %d taker observations across %d tokens", seen, min(20, len(token_ids)))


# D96-NEW-1a: Triggered identity poll for single market
async def _poll_single_market_identity(
    asset_id: str,
    market_id: str,
    db: ShadowDB,
    label: str = "triggered",
    market_tier: str = "t3",
) -> int:
    """
    Immediately execute one identity poll for a single market.
    Returns the number of newly written wallet_observations.

    cursor: only trades with timestamp > _last_poll_ts[asset_id] (client-side filter)
    dedup: INSERT OR IGNORE by transaction_hash (if hash present)
    """
    global _last_poll_ts

    url = "https://data-api.polymarket.com/trades"
    params = {"asset_id": asset_id, "limit": 100, "takerOnly": "true"}

    try:
        loop = asyncio.get_event_loop()
        # urllib.request is async-compatible via run_in_executor
        def _http_get() -> tuple[int, list]:
            q = urllib.parse.urlencode(params)
            full_url = f"{url}?{q}"
            req = urllib.request.Request(
                full_url,
                headers={"Accept": "application/json", "User-Agent": "panopticon-radar/1.0"},
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return (resp.status, json.loads(resp.read().decode("utf-8")))

        status, trades = await loop.run_in_executor(None, _http_get)
        if status >= 400:
            logger.warning("[POLL][%s] HTTP %s for %s", label, status, asset_id[:20])
            return 0
        if not isinstance(trades, list):
            return 0

        last_ts = _last_poll_ts.get(asset_id, 0)
        new_count = 0

        for raw in trades:
            ts_ms = int(raw.get("timestamp") or 0)
            if ts_ms <= last_ts:
                continue  # cursor: already processed

            proxy_wallet = raw.get("proxyWallet") or raw.get("taker_address") or ""
            tx_hash = raw.get("transactionHash") or ""
            size = float(raw.get("size") or 0)
            price = float(raw.get("price") or 0)
            side = raw.get("side", "")
            usdc_size = round(size * price, 4)

            # RULE-DATA-2: mandatory three-field validation
            if not proxy_wallet or usdc_size <= 0:
                continue

            # order reconstruction
            order_id = try_match_or_open(raw, db)

            # wallet_observations with transaction_hash dedup
            if tx_hash:
                db.conn.execute("""
                    INSERT OR IGNORE INTO wallet_observations
                        (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc, transaction_hash, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(uuid4()),
                    proxy_wallet.lower()[:42],
                    market_id,
                    "clob_trade",
                    json.dumps({"side": side, "size": size, "price": price, "source": f"data_api_{label}"}, ensure_ascii=False),
                    _utc(),
                    tx_hash,
                    order_id,
                ))
            else:
                db.conn.execute("""
                    INSERT INTO wallet_observations
                        (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc, transaction_hash, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(uuid4()),
                    proxy_wallet.lower()[:42],
                    market_id,
                    "clob_trade",
                    json.dumps({"side": side, "size": size, "price": price, "source": f"data_api_{label}"}, ensure_ascii=False),
                    _utc(),
                    tx_hash,
                    order_id,
                ))
            new_count += 1

        db.conn.commit()

        if trades:
            max_ts = max(int(t.get("timestamp") or 0) for t in trades)
            _last_poll_ts[asset_id] = max(last_ts, max_ts)

        # ── D81: Identity Coverage 量測 ──────────────────────────────────────
        # Ground truth：CLOB WS 全域計數器（模組層級，60s 滾動窗口）
        ws_ticks = _ws_trade_count

        # data-api 觀測
        api_received    = len(trades)
        api_with_wallet = sum(
            1 for t in trades
            if (t.get("proxyWallet") or t.get("taker_address") or "")
            not in ("", "0x0000000000000000000000000000000000000000")
        )
        page_saturated = int(api_received >= 100)

        # 損失率計算
        loss_rate  = max(0.0, 1.0 - api_received / ws_ticks) if ws_ticks > 0 else None
        wallet_cov = (api_with_wallet / api_received) if api_received > 0 else 0.0

        # 累計統計（讀上一筆）
        prev_row = db.conn.execute(
            """
            SELECT event_total_ws_ticks, event_total_api_trades
            FROM identity_coverage_log
            WHERE market_id=? AND window_ts=0
            ORDER BY created_at DESC LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        total_ws  = (prev_row[0] if prev_row else 0) + ws_ticks
        total_api = (prev_row[1] if prev_row else 0) + api_received
        cum_loss  = max(0.0, 1.0 - total_api / total_ws) if total_ws > 0 else None

        db.write_identity_coverage({
            "market_id":              market_id,
            "asset_id":              asset_id,
            "window_ts":             0,
            "market_tier":           market_tier,
            "event_slug":            "",
            "window_start_utc":      _utc(),
            "window_end_utc":        _utc(),
            "poll_interval_sec":     4.0,
            "ws_trade_ticks":       ws_ticks,
            "api_trades_received":  api_received,
            "api_trades_with_wallet": api_with_wallet,
            "estimated_loss_rate":  loss_rate,
            "wallet_coverage_rate": wallet_cov,
            "api_page_saturated":   page_saturated,
            "event_total_ws_ticks": total_ws,
            "event_total_api_trades": total_api,
            "event_cumulative_loss": cum_loss,
            "created_at":            _utc(),
        })

        # 警告日誌
        if page_saturated:
            logger.warning(
                "[COVERAGE][SATURATED] market=%s label=%s ws=%d api=%d → truncation confirmed",
                market_id[:20], label, ws_ticks, api_received
            )
        if loss_rate is not None and loss_rate > 0.30:
            logger.warning(
                "[COVERAGE][HIGH_LOSS] market=%s loss=%.1f%% wallet_cov=%.1f%%",
                market_id[:20], loss_rate * 100, wallet_cov * 100
            )

        logger.info("[POLL][%s] market=%s new=%d", label, market_id[:20], new_count)
        return new_count

    except Exception as e:
        logger.warning("[POLL][%s] error: %s", label, e)
        return 0


def try_match_or_open(raw: dict, db) -> str:
    """
    Redirect to order_reconstruction_engine.try_match_or_open.
    Kept as module-level alias for backward compatibility with _poll_data_api_for_takers.
    """
    from panopticon_py.ingestion.order_reconstruction_engine import try_match_or_open as _impl
    return _impl(raw, db)



_ws_raw_msg_count = 0
_ws_trade_count = 0
_ws_real_trade_count = 0  # D125: embedded book trades + last_trade_price (may overlap same fill)
_ws_entropy_fire_count = 0
_ws_kyle_sample_count = 0  # D9: Kyle λ samples from book_embedded + standalone
_ws_last_stream_msg_ts = 0.0  # D123: timestamp of last message from stream_json_messages
_last_ws_diag_log_ts = 0.0
_WS_DIAG_LOG_INTERVAL_SEC = 60.0
_FIRST_TRADE_TICK_LOGGED = False  # Task C: one-time TRADE_TICK diagnostic
# D81: Heartbeat loop accumulators (initialized at module level, written by while loop)
_live_loop_started = 0.0
_d75_hb_last = 0.0
_d75_hb_trade_base = 0
_d75_hb_real_trade_base = 0
_d75_hb_entropy_base = 0
_d77_tick_last = 0.0
_d77_tick_n = 0

# D96-NEW-1a/1b: Triggered identity poll state
_triggered_poll_cooldown: dict[str, float] = {}   # market_id -> last trigger monotonic ts
_last_poll_ts: dict[str, int] = {}              # token_id -> last seen trade Unix ms
LARGE_TRADE_TRIGGER_USDC = 500.0
TRIGGERED_POLL_COOLDOWN_SEC = 2.0


def _pctl(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    arr = sorted(values)
    idx = int(round((len(arr) - 1) * pct))
    idx = max(0, min(idx, len(arr) - 1))
    return arr[idx]


async def _live_ticks(ew: EntropyWindow, db: ShadowDB, signal_queue: asyncio.Queue | None = None) -> None:
    """
    Radar live tick loop with TWO collection layers:

    1. WS feed (fast): subscribes to ALL 200 active Polymarket tokens.
       Listens for book/price_change events to detect entropy drops.
       Runs continuously; reconnects every 10s to refresh subscription.

    2. Data API polling (slow): polls /trades for top 20 tokens every 30s
       to capture real taker addresses (proxyWallet) missing from WS events.

    Key design: the outer heartbeat loop runs WS in short 10s bursts,
    avoids CancelledError propagation bugs from nested awaits.
    """
    from panopticon_py.hunting.clob_ws_client import run_ws_loop

    global _last_ws_diag_log_ts, _live_loop_started
    global _d75_hb_last, _d77_tick_last, _d77_tick_n
    global _d75_hb_trade_base, _d75_hb_entropy_base
    global _last_pol_refresh  # D112: added missing declaration — crash at L2736 if absent

    # ── MetricsCollector: get singleton + baseline sync ──────────────────────────
    mc = _mc()
    if mc is not None:
        _sync_metrics_baseline(db, mc)

    # FIX-1: Initialize T1 window state immediately on startup
    # (on_t1_window_rollover only fires at 5-min boundaries, so we call it now)
    try:
        from panopticon_py.hunting.t1_market_clock import get_current_t1_window
        _now = datetime.now(timezone.utc)
        _t1_start_ts = int((_now.timestamp() // 300) * 300)
        _t1_end_ts = _t1_start_ts + 300
        _t1_secs_left = max(0, _t1_end_ts - int(_now.timestamp()))
        if mc is not None:
            mc.on_t1_window_rollover(
                window_start=_t1_start_ts,
                window_end=_t1_end_ts,
                secs_remaining=float(_t1_secs_left),
            )
        logger.info("[STARTUP][T1_WINDOW] ts=%d secs_left=%d", _t1_start_ts, _t1_secs_left)
    except Exception as e:
        logger.warning("[STARTUP][T1_WINDOW][ERROR] %s", e)

    # ── Start 5s JSON write loop (caller passes db explicitly) ──────────────────
    if mc is not None:
        asyncio.create_task(
            _metrics_json_loop(mc, db, path="data/rvf_live_snapshot.json"),
            name="metrics-json-loop",
        )

    recent: list[dict] = []
    _msg_count = 0
    _close_event = asyncio.Event()

    # ── Kyle's Lambda tracking ──────────────────────────────────────────────
    # Per-asset book snapshot: mid_price before each trade (for lambda calculation)
    _book_snapshot: dict[str, dict] = {}  # asset_id -> {"mid": float, "ts": str}
    _pending_trade: dict[str, dict] = {}  # asset_id -> {"size", "price", "ts", "mid_before", "expire_ts"}
    _PENDING_TRADE_TTL_SEC = 30.0  # stale entry TTL
    # D75/D81: _evt_count and _entropy_* are now module-level (initialized above _live_ticks)
    # Only reset them here on function entry (they were local vars before this fix).
    _evt_count = {"last_trade_price": 0, "book": 0, "price_change": 0, "other": 0}
    _entropy_eval_total = 0
    _entropy_locked_count = 0
    _entropy_history_not_ready_count = 0
    _entropy_z_ready_count = 0
    _entropy_z_below_threshold_count = 0
    _entropy_z_samples = []

    # ── Message handler ────────────────────────────────────────────────────────
    async def _on_message(msg: dict | list) -> None:
        nonlocal _msg_count
        nonlocal _evt_count, _entropy_eval_total, _entropy_locked_count
        nonlocal _entropy_history_not_ready_count, _entropy_z_ready_count
        nonlocal _entropy_z_below_threshold_count, _entropy_z_samples

        # P2 DIAG: WebSocket L1 counters — do NOT modify business logic
        global _ws_raw_msg_count, _ws_trade_count, _ws_real_trade_count, _ws_entropy_fire_count, _ws_kyle_sample_count

        # Polymarket WS sends both dict messages and list batches (multiple market
        # updates in one frame).  Normalize to a list for uniform processing.
        batch: list[dict] = msg if isinstance(msg, list) else [msg]

        for item in batch:
            if not isinstance(item, dict):
                continue

            # D29: elapsed_since_last_ws_msg fix — update on every WS frame
            # before any event-type filtering so snapshot staleness does not drift.
            mc = _mc()
            if mc is not None:
                try:
                    mc.on_ws_message()
                except Exception:
                    pass

            # ── P2 DIAG: raw message counter ──────────────────────────────────
            _ws_raw_msg_count += 1

            recv = time.monotonic()
            event_type = item.get("event_type", "")
            if event_type in _evt_count:
                _evt_count[event_type] += 1
            else:
                _evt_count["other"] += 1

            # ── [DIAG][WS_EVENT_RAW] for T1 assets — confirms T1 event types ─────
            asset_id_top = item.get("asset_id") or item.get("market") or ""
            tier_top = _token_tier_map.get(asset_id_top, "unknown")
            if tier_top == "t1":
                # D122-3: INFO level so we can see actual event types in production logs
                logger.info(
                    "[DIAG][WS_EVENT_RAW] type=%s asset=%s keys=%s",
                    event_type,
                    asset_id_top[:20] if asset_id_top else "None",
                    str(list(item.keys()))[:120],
                )

            if _msg_count <= 5:
                logger.debug(
                    "[RADAR DEBUG] Msg #%d keys=%s event_type=%s",
                    _msg_count,
                    list(item.keys()),
                    event_type,
                )

            buy = 0.0
            sell = 0.0

            # ── book event: Kyle's λ data source + mid_price snapshot ─────────────
            # Per Polymarket docs: "emitted when there is a trade that affects the book"
            # So book events ARE triggered by trades → embedded last_trade_price available
            # Invariant 1.1: book events are NOT pushed to EntropyWindow (continue after snapshot)
            if event_type == "book":
                try:
                    mc.on_book_event()
                except Exception:
                    pass
                bids: list[dict] = item.get("bids") or []
                asks: list[dict] = item.get("asks") or []
                asset = item.get("asset_id") or item.get("market") or ""

                # Step 1: Calculate mid_now
                mid_now = None
                if bids and asks:
                    try:
                        best_bid = float(bids[0].get("price") or 0)
                        best_ask = float(asks[0].get("price") or 0)
                        if best_bid > 0 and best_ask > best_bid:
                            mid_now = (best_bid + best_ask) / 2.0
                    except (TypeError, ValueError):
                        pass

                # Step 2: Extract embedded last_trade_price (Kyle λ trigger point)
                embedded_trade_price = None
                embedded_trade_size = 0.0
                try:
                    _etp = item.get("last_trade_price")
                    if _etp is not None:
                        embedded_trade_price = float(_etp)
                    _ets = item.get("size")
                    if _ets is not None:
                        embedded_trade_size = float(_ets)
                except (TypeError, ValueError):
                    embedded_trade_price = None
                    embedded_trade_size = 0.0

                # Step 2b: Fire on_trade_tick for every book event (either real trade or book update)
                # Polymarket emits book events for all trades; if no embedded trade, count as book update
                try:
                    mc.on_trade_tick()
                except Exception:
                    pass

                # D122 (REVERTED D124): Count ALL book events in _ws_trade_count.
                # BTC 5m book events ALWAYS carry embedded last_trade_price.
                # The original "if not embedded_trade_price" guard caused trade_ticks_60s=0.
                # D124: Count every book event — heartbeat should reflect WS activity,
                # not just "real trades without a companion last_trade_price event".
                global _ws_trade_count
                _ws_trade_count += 1
                # D125: Narrow counter — embedded trade field present (may overlap last_trade_price)
                if embedded_trade_price is not None:
                    _ws_real_trade_count += 1
                    try:
                        mc.on_real_trade_tick()
                    except Exception:
                        pass

                # Step 3: Kyle λ calculation from embedded trade (D9 APPROVED)
                # NOTE: book events may NOT contain last_trade_price in practice.
                # See D9-OBS: Polymarket CLOB book events carry no embedded trade data.
                if embedded_trade_price and embedded_trade_size > 0 and mid_now is not None:
                    pending = _pending_trade.get(asset)
                    # Stale TTL cleanup
                    if pending and recv > pending.get("expire_ts", 0):
                        del _pending_trade[asset]
                        pending = None
                    # mid_before guard (first subscription has no book snapshot)
                    if pending and pending.get("mid_before") is None:
                        del _pending_trade[asset]
                        pending = None

                    if pending and pending["size"] > 0:
                        mid_before = pending["mid_before"]  # guaranteed non-None
                        delta_p = abs(mid_now - mid_before)
                        delta_v = embedded_trade_size
                        if delta_v > 0:
                            lambda_obs = delta_p / delta_v
                            # D97: Guard window_ts=0 only for T1 markets.
                            # T2/T3/T5 slugs may not end in digits — compute slug/window_ts locally.
                            slug_book = _token_to_slug_map.get(asset, "")
                            ts_part_book = slug_book.rsplit("-", 1)[-1] if slug_book and "-" in slug_book else ""
                            window_ts = int(ts_part_book) if ts_part_book.isdigit() else 0
                            tier_for_kyle = _token_tier_map.get(asset, "t3")
                            if tier_for_kyle == "t1" and window_ts == 0:
                                logger.info(
                                    "[KYLE_SKIP][NO_WINDOW_TS] T1 asset=%s slug=%s — "
                                    "window_ts unknown, skipping kyle sample",
                                    asset[:20], slug_book[:40] if slug_book else "UNKNOWN",
                                )
                            else:
                                db.append_kyle_lambda_sample({
                                    "asset_id": asset,
                                    "ts_utc": pending["ts"],
                                    "delta_price": delta_p,
                                    "trade_size": delta_v,
                                    "lambda_obs": lambda_obs,
                                    "market_id": asset,
                                    "source": "book_embedded",
                                    "window_ts": window_ts,
                                })
                                _ws_kyle_sample_count += 1
                        del _pending_trade[asset]
                    else:
                        # No pending → record mid_before for next book event
                        _pending_trade[asset] = {
                            "mid_before": mid_now,
                            "trade_price": embedded_trade_price,
                            "size": embedded_trade_size,
                            "ts": normalize_external_ts_to_utc(item.get("timestamp")) if item.get("timestamp") else _utc(),
                            "expire_ts": recv + _PENDING_TRADE_TTL_SEC,
                        }
                        logger.debug(
                            "[DIAG][BOOK_MID_SET] asset=%s mid=%.6f",
                            asset[:20], mid_now,
                        )
                else:
                    # No embedded trade → just update snapshot
                    pass

                # Step 4: Always update book snapshot
                if mid_now is not None:
                    _book_snapshot[asset] = {"mid": mid_now, "ts": _utc()}
                continue  # ⛔ Invariant 1.1: book events NEVER call ew.push()

            # ── T1 auto-resub: market_resolved event fires when a T1 market closes ──
            # Every 5 min the BTC 5-min market expires → immediately refresh T1 tokens
            # so the next open market is subscribed without waiting for the next heartbeat.
            if event_type == "market_resolved":
                resolved_asset = item.get("asset_id") or (item.get("assets_ids") or [None])[0]
                if resolved_asset and _token_tier_map.get(resolved_asset) == "t1":
                    slug = _token_to_slug_map.get(resolved_asset, "unknown")
                    window_ts = slug.rsplit("-", 1)[-1] if slug and "-" in slug else "?"
                    logger.info(
                        "[T1_RESOLVED] asset=%s window_ts=%s slug=%s — triggering T1 refresh",
                        str(resolved_asset)[:20], window_ts, slug,
                    )
                    asyncio.create_task(_refresh_tier1_tokens(db))
                continue

            # ── P1-FIX: tick_size_change — critical for bots per Polymarket WS spec ────
            # "If tick size changes and you use the old one, orders are rejected."
            if event_type == "tick_size_change":
                try:
                    old_tick = item.get("old_tick_size")
                    new_tick = item.get("new_tick_size")
                    asset = item.get("asset_id") or item.get("market") or ""
                    logger.warning(
                        "[TICK_SIZE_CHANGE] asset=%s old=%s new=%s — "
                        "if live orders placed, must update tick size",
                        str(asset)[:20], old_tick, new_tick,
                    )
                except Exception:
                    pass
                continue

            # ── P1-FIX: price_change — emitted when orders placed/cancelled ─────────────
            # This is a Quote-Tick (like book); MM can cancel at will.
            # Do NOT route to EntropyWindow or Kyle lambda — use only for book snapshot.
            if event_type == "price_change":
                try:
                    price_changes = item.get("price_changes") or []
                    for pc in price_changes:
                        asset = pc.get("asset_id") or pc.get("market") or ""
                        best_bid = pc.get("best_bid")
                        best_ask = pc.get("best_ask")
                        if best_bid and best_ask:
                            try:
                                bid_f = float(best_bid)
                                ask_f = float(best_ask)
                                mid = (bid_f + ask_f) / 2.0
                                if asset not in _book_snapshot:
                                    _book_snapshot[asset] = {}
                                _book_snapshot[asset]["mid"] = mid
                                _book_snapshot[asset]["bid"] = bid_f
                                _book_snapshot[asset]["ask"] = ask_f
                                _book_snapshot[asset]["ts"] = recv
                            except (TypeError, ValueError):
                                pass
                except Exception:
                    pass
                continue

            # ── Invariant 1.1: Entropy calculation MUST use Trade-Tick (last_trade_price) ──
            # book / price_change are Quote-Tick (order book updates) — MM can cancel at will.
            # last_trade_price is emitted ONLY when a maker and taker order is matched (real trade).
            if event_type == "last_trade_price":
                try:
                    mc.on_trade_tick()
                except Exception:
                    pass
                try:
                    trade_size = float(item.get("size") or 0)
                except (TypeError, ValueError):
                    trade_size = 0.0
                trade_side = str(item.get("side") or "").upper()
                if trade_size == 0:
                    continue
                _ws_trade_count += 1
                _ws_real_trade_count += 1
                try:
                    mc.on_real_trade_tick()
                except Exception:
                    pass

                # Capture mid_before from snapshot BEFORE any update
                asset_id = item.get("asset_id") or item.get("market") or ""
                # market_id: Polymarket WS uses asset_id as the market identifier for last_trade_price events.
                # This local assignment fixes a pre-existing gap where market_id was used
                # in entropy-fire handling without being set from the current item.
                market_id = asset_id
                mid_before = _book_snapshot.get(asset_id, {}).get("mid")
                trade_price = float(item.get("price") or 0)
                trade_ts = normalize_external_ts_to_utc(item.get("timestamp")) if item.get("timestamp") else _utc()

                # D81: TE target push — Polymarket CLOB last_trade_price (O(1), non-blocking)
                te = _te()
                if te is not None:
                    te.push_target(trade_price)

                # ── [DIAG][T1_TICK] Per-trade diagnostic for T1 markets ───────────────
                tier = _token_tier_map.get(asset_id, "t3")
                if tier == "t1":
                    from panopticon_py.hunting.t1_market_clock import (
                        get_corrected_unix_time, get_current_t1_window,
                    )
                    slug = _token_to_slug_map.get(asset_id, "unknown")
                    w_ts_str = slug.rsplit("-", 1)[-1] if slug and "-" in slug else "?"
                    try:
                        w_ts = int(w_ts_str)
                        secs_left = (w_ts + 300) - int(get_corrected_unix_time())
                    except (ValueError, TypeError):
                        secs_left = -1
                    from panopticon_py.hunting import t1_market_clock as t1c
                    logger.debug(
                        "[DIAG][T1_TICK] asset=%s price=%s size=%s "
                        "window_ts=%s secs_left=%ds ntp_offset=%.3fs",
                        asset_id[:20], trade_price, trade_size,
                        w_ts_str, secs_left, getattr(t1c, "_ntp_offset_seconds", 0.0),
                    )
                    # D96-B: T1 route — push to per-token ew only; NO signal queue, NO fire, NO DB write
                    t1_ew = _entropy_windows.setdefault(asset_id, EntropyWindow())
                    # Derive buy/sell for H_sample
                    t1_buy = trade_size if trade_side == "BUY" else 0.0
                    t1_sell = trade_size if trade_side == "SELL" else 0.0
                    t1_ew.push(recv, t1_buy, t1_sell)
                    t1_ew.record_H_sample(recv)
                    # D96-B: STOP — Kyle λ can still compute (mid_before already captured above)
                    # but T1 does NOT go through consensus pipeline
                    continue

                # ── Kyle λ calculation (standalone path) ─────────────────────────────────
                # Compute lambda using mid_before from the book snapshot captured above.
                # _pending_trade stores trade_price so the NEXT book event can also compute.
                if mid_before is None:
                    # [DIAG][KYLE_SKIP] — first trade tick has no prior book snapshot
                    logger.info(
                        "[DIAG][KYLE_SKIP] asset=%s reason=mid_before_none pending_state=%s",
                        asset_id[:20],
                        str(_pending_trade.get(asset_id, "NOT_FOUND"))[:80],
                    )
                    mc = _mc()
                    if mc is not None:
                        mc.on_kyle_skip()
                    # Initialize state — mid_before will be set on NEXT trade
                    _pending_trade[asset_id] = {
                        "size": 0.0, "price": 0.0, "ts": trade_ts,
                        "mid_before": None, "expire_ts": recv + _PENDING_TRADE_TTL_SEC,
                    }
                elif mid_before is not None and trade_price > 0:
                    pending = _pending_trade.get(asset_id)
                    # Stale TTL cleanup
                    if pending and recv > pending.get("expire_ts", 0):
                        del _pending_trade[asset_id]
                        pending = None
                    pending_price = _pending_trade_price(pending)
                    if pending and pending.get("mid_before") is not None and pending_price > 0:
                        # We have a previous trade's price → compute lambda
                        prev_price = pending_price
                        delta_p = abs(trade_price - prev_price)
                        delta_v = trade_size
                        if delta_v > 0:
                            lambda_obs = delta_p / delta_v
                            # D96-NEW-3: upgrade to order-level size if order_id is known
                            pending_oid = pending.get("order_id")
                            if pending_oid:
                                row = db.conn.execute(
                                    "SELECT total_size FROM order_reconstructions WHERE order_id=? LIMIT 1",
                                    (pending_oid,),
                                ).fetchone()
                                if row:
                                    effective_size = float(row[0])
                                    lambda_obs = delta_p / max(effective_size, 0.01)
                                    logger.debug(
                                        "[KYLE][D96] order_id=%s total_size=%.4f lambda=%.6f",
                                        pending_oid[:16], effective_size, lambda_obs,
                                    )
                            # D97: Guard window_ts=0 only for T1 markets.
                            # T2/T3/T5 slugs may not end in digits — use window_ts=0 as fallback.
                            # Compute slug/window_ts here (slug used in log below; window_ts for guard).
                            slug_for_kyle = _token_to_slug_map.get(asset_id, "")
                            ts_part_k = slug_for_kyle.rsplit("-", 1)[-1] if slug_for_kyle and "-" in slug_for_kyle else ""
                            window_ts = int(ts_part_k) if ts_part_k.isdigit() else 0
                            tier_for_kyle = _token_tier_map.get(asset_id, "t3")
                            if tier_for_kyle == "t1" and window_ts == 0:
                                logger.info(
                                    "[KYLE_SKIP][NO_WINDOW_TS] T1 asset=%s slug=%s — "
                                    "window_ts unknown, skipping kyle sample",
                                    asset_id[:20], slug_for_kyle[:40] if slug_for_kyle else "UNKNOWN",
                                )
                            else:
                                db.append_kyle_lambda_sample({
                                    "asset_id": asset_id,
                                    "ts_utc": trade_ts,
                                    "delta_price": delta_p,
                                    "trade_size": delta_v,
                                    "lambda_obs": lambda_obs,
                                    "market_id": asset_id,
                                    "source": "standalone",
                                    "window_ts": window_ts,
                                })
                                _ws_kyle_sample_count += 1
                                mc = _mc()
                                if mc is not None:
                                    mc.on_kyle_compute(asset_id, lambda_obs)
                                logger.info(
                                    "[DIAG][KYLE] standalone asset=%s lambda=%.6f delta_p=%.4f size=%.2f",
                                    asset_id[:20], lambda_obs, delta_p, trade_size,
                                )
                                logger.debug(
                                    "[DIAG][KYLE_COMPUTE] asset=%s mid_before=%.6f "
                                    "trade_price=%.6f size=%.4f",
                                    asset_id[:20], mid_before, trade_price, trade_size,
                                )

                # D96-NEW-3: track order_id per pending trade for downstream Kyle λ upgrade
                # Initialize order_id = None so it's in scope for the entire _pending_trade block below.
                order_id = None
                pending_order_id = _pending_trade.get(asset_id, {}).get("order_id")
                existing = _pending_trade.get(asset_id)
                _pending_trade[asset_id] = {
                    "size": trade_size,
                    "price": trade_price,  # D31: keep current trade's price as prev for next trade
                    "ts": trade_ts,
                    "mid_before": (
                        existing["mid_before"]
                        if existing and existing.get("mid_before") is not None
                        else mid_before
                    ),
                    "expire_ts": recv + _PENDING_TRADE_TTL_SEC,
                    "order_id": order_id if order_id else pending_order_id,
                }

                if trade_side == "BUY":
                    buy = trade_size
                    sell = 0.0
                elif trade_side == "SELL":
                    buy = 0.0
                    sell = trade_size
                else:
                    continue  # unknown side, discard

                # Task C: One-time diagnostic log for first last_trade_price event
                global _FIRST_TRADE_TICK_LOGGED
                if not _FIRST_TRADE_TICK_LOGGED:
                    logger.info(
                        "[DIAG][TRADE_TICK] FIRST trade tick — asset_id=%s side=%s size=%s price=%s timestamp=%s",
                        item.get("asset_id", "?"),
                        trade_side,
                        trade_size,
                        item.get("price", "?"),
                        item.get("timestamp", "?"),
                    )
                    _FIRST_TRADE_TICK_LOGGED = True

                # D96-NEW-1a: Pre-fire triggered poll for large T2/T3/T5 trades
                trade_usdc = trade_price * trade_size
                if trade_usdc >= LARGE_TRADE_TRIGGER_USDC:
                    now_mono = time.monotonic()
                    if now_mono - _triggered_poll_cooldown.get(market_id, 0) > TRIGGERED_POLL_COOLDOWN_SEC:
                        _triggered_poll_cooldown[market_id] = now_mono
                        asyncio.create_task(
                            _poll_single_market_identity(asset_id, market_id, db, label="pre_fire", market_tier=tier)
                        )
                    # D96-NEW-3: sync order reconstruction for the CURRENT trade
                    # compute order_id now so _pending_trade has it for the next Kyle λ sample
                    order_id = try_match_or_open({
                        "proxyWallet": item.get("ws_address") or item.get("taker_address") or "",
                        "conditionId": asset_id,
                        "side": trade_side,
                        "price": trade_price,
                        "size": trade_size,
                        "timestamp": int(time.time() * 1000),
                    }, db)
                else:
                    order_id = None

                # Push to EntropyWindow (Trade-Tick only)
                ew.push(recv, buy, sell)
                ew.record_H_sample(recv)
                # D75: Entropy gate pre/post diagnostics to explain why fire doesn't happen.
                _entropy_eval_total += 1
                _d_diag, z_diag = ew.zscore_of_latest_delta()
                state_diag = ew.state_dict()
                if z_diag is None:
                    if state_diag.get("trigger_locked"):
                        _entropy_locked_count += 1
                    else:
                        _entropy_history_not_ready_count += 1
                else:
                    _entropy_z_ready_count += 1
                    _entropy_z_samples.append(float(z_diag))
                    if z_diag < get_z_threshold():
                        _entropy_z_below_threshold_count += 1

                # ── Task D: INSIDER_PATTERN_COLLECTOR — forensic only, no signal path ──
                # Extract taker address: WS last_trade_price events carry no taker_address.
                # _poll_data_api_for_takers populates wallet_observations with real proxyWallet.
                # Use ws_address field if present; otherwise fall back to 'ws_unknown'.
                taker_addr = str(item.get("ws_address") or item.get("taker_address") or "ws_unknown")[:42].lower()
                token_id_for_pattern = item.get("asset_id") or item.get("market") or ""
                if taker_addr != "ws_unknown":
                    first_seen = db.get_wallet_first_seen(taker_addr) or _utc()
                    try:
                        result = compute_pattern_score(
                            wallet_address=taker_addr,
                            asset_id=token_id_for_pattern,
                            stake_usd=float(trade_size),
                            market_prior=1.0 - trade_price,
                            account_first_seen_ts=first_seen,
                            db_conn=db.conn,
                        )
                        if result["score"] >= 0.70:
                            db.insert_insider_pattern_flag(
                                wallet_address=taker_addr,
                                asset_id=token_id_for_pattern,
                                detected_ts_utc=_utc(),
                                case_type=result["case_type"],
                                account_age_days=result["factors"].get("account_age", 0.0),
                                prior_at_bet=result["factors"].get("prior_conviction", 0.0),
                                stake_usd=float(trade_size),
                                correlated_mkts=result["correlated_mkts"],
                                cluster_wallet_count=result["cluster_wallet_count"],
                                same_ts_wallets=result["same_ts_wallets"],
                                has_decoy_bets=int(result["has_decoy_bets"]),
                                pattern_score=result["score"],
                                flag_reason="AUTO_SCORE_GTE_0.70",
                            )
                            logger.warning(
                                "[INSIDER_PATTERN_ALERT] wallet=%s asset=%s score=%.2f case=%s factors=%s",
                                taker_addr[:20],
                                token_id_for_pattern[:20],
                                result["score"],
                                result["case_type"],
                                result["factors"],
                            )
                    except Exception:
                        pass  # forensic only — never crash the tick path

                if ew.should_fire_negative_entropy(get_z_threshold()):
                    # ── P2 DIAG: entropy fire counter ────────────────────────────────
                    _ws_entropy_fire_count += 1

                    recent.append(msg)
                    parents, virtuals = cross_wallet_burst_cluster(recent[-50:])
                    z_score = ew.zscore_of_latest_delta()

                    # D37 FIX: Tell MetricsCollector entropy fire detected (for active_entropy_windows + mean_z + processed_60s)
                    # NOTE: must call AFTER z_score is computed above
                    mc = _mc()
                    if mc is not None:
                        mc.on_entropy_fire(z_score[1] if z_score[1] is not None else 0.0)
                        mc.on_signal_processed()  # D37 FIX: count entropy fires as "processed" signals

                    # ── P3 DIAG: log actual z-score for every entropy fire ─────────────
                    diag_z = z_score[1] if z_score[1] is not None else 999.0
                    logger.info(
                        "[DIAG][ENTROPY_FIRE] z=%.3f fire=YES market=%s taker=%s",
                        diag_z,
                        market_id,
                        taker_addr[:20],
                    )
                    obs_payload = {
                        "obs_id": str(uuid4()),
                        "address": taker_addr,
                        "market_id": market_id,
                        "obs_type": "entropy_drop",
                        "payload_json": {
                            "entropy_z": z_score[1],
                            "sim_pnl_proxy": z_score[0],
                            "msg_keys": list(msg.keys())[:12],
                            "virtual_entities": len(virtuals),
                        },
                        "ingest_ts_utc": _utc(),
                    }
                    db.append_wallet_observation(obs_payload)
                    db.append_hunting_shadow_hit({
                        "hit_id": str(uuid4()),
                        "address": taker_addr,
                        "market_id": market_id,
                        "entity_score": None,
                        "entropy_z": z_score[1],
                        "sim_pnl_proxy": z_score[0],
                        "outcome": None,
                        "payload_json": {"msg_keys": list(msg.keys())[:12], "virtual_entities": len(virtuals)},
                        "created_ts_utc": _utc(),
                    })

                    # D96-NEW-1b: On-fire forced poll — pre-emptively fetch identity data
                    # before signal_engine processes this event (grace period will handle timing)
                    asyncio.create_task(
                        _poll_single_market_identity(asset_id, market_id, db, label="on_fire", market_tier=tier)
                    )

                    # ── C2: D21 Phase 2 — Catalyst detection hook for T2 markets ──────────
                    # Record catalyst event and trigger async backward lookback (fire-and-forget)
                    if tier == "t2" and z_score[1] is not None:
                        try:
                            db.write_catalyst_event({
                                "market_id": market_id,
                                "slug": _token_to_slug_map.get(token_id, market_id),
                                "z_score": z_score[1],
                                "prob_before": mid_before or 0.5,
                                "prob_after": mid_now or 0.5,
                                "prob_delta": (mid_now or 0.5) - (mid_before or 0.5),
                            })
                        except Exception:
                            pass  # non-critical
                        asyncio.create_task(
                            _backward_lookback(
                                market_id=market_id,
                                token_id=token_id,
                                catalyst_ts=time.time(),
                                prob_before=mid_before or 0.5,
                            )
                        )

                    # Queue SignalEvent to signal_engine — ZERO disk I/O [Invariant 1.1]
                    # Fallback to DB only when queue is not available (backward compat)
                    if signal_queue is not None:
                        token_id = msg.get("asset_id") or msg.get("token_id") or market_id
                        tier = _token_tier_map.get(token_id, "t3")
                        # ── Q12: Propagate series_id + window_ts for T1 markets ─────
                        slug = _token_to_slug_map.get(token_id, "")
                        series_id = ""
                        window_ts = 0
                        if slug and tier == "t1":
                            ts_part = slug.rsplit("-", 1)[-1]
                            series_id = slug.rsplit(f"-{ts_part}", 1)[0] if ts_part.isdigit() else slug
                            window_ts = int(ts_part) if ts_part.isdigit() else 0
                        # D117: T5 time-decay weighting
                        time_to_event = 0.0
                        if tier == "t5":
                            end_ts = _token_endtime_map.get(token_id, 0.0)
                            if end_ts > 0:
                                time_to_event = max(0.0, end_ts - time.time())
                        await signal_queue.put(SignalEvent(
                            source="radar",
                            market_id=market_id,
                            token_id=token_id,
                            entropy_z=z_score[1],
                            trigger_address=taker_addr,
                            trigger_ts_utc=_utc(),
                            market_tier=tier,
                            series_id=series_id,
                            window_ts=window_ts,
                            time_to_event=time_to_event,
                        ))
                        logger.debug("[RADAR→SE] entropy_z=%.2f market=%s queued", z_score[1], market_id)
                    else:
                        # Backward compat: write to DB only when queue unavailable
                        db.append_pending_entropy_signal({
                            "signal_id": str(uuid4()),
                            "market_id": market_id,
                            "token_id": msg.get("asset_id") or msg.get("token_id") or market_id,
                            "entropy_z": z_score[1],
                            "sim_pnl_proxy": z_score[0],
                            "trigger_address": taker_addr,
                            "trigger_ts_utc": _utc(),
                        })

    # ── Boot: NTP sync + clock validation ─────────────────────────────
    from panopticon_py.hunting.t1_market_clock import (
        sync_ntp_offset, validate_clock_against_anchor,
    )
    sync_ntp_offset()  # blocks max 3s — acceptable at startup
    validate_clock_against_anchor()

    # ── Boot: concurrent token load from all tiers via asyncio.gather ──────
    global _current_tokens
    combined_tokens, tier1_tokens, tier2_tokens, tier5_tokens, tier3_tokens = (
        await _refresh_all_subscriptions(db)
    )
    _current_tokens = combined_tokens
    _close_event.clear()
    ew.mark_reconnect()

    logger.info(
        "[L1_MARKET_TIER] tier1=%d tier2_event=%d tier5_sports=%d tier3_long=%d total=%d",
        len(tier1_tokens), len(tier2_tokens), len(tier5_tokens), len(tier3_tokens),
        len(combined_tokens),
    )

    if _current_tokens:
        # D121: Build WS token list within payload size limit for initial subscription
        ws_tokens = _build_ws_token_list(
            _cached_t1_tokens,
            _cached_t2_tokens,
            limit=_WS_TOKEN_LIMIT,
        )
        sub = {"assets_ids": ws_tokens, "type": "market", "custom_feature_enabled": True}
        # D121-3: Payload size pre-check for initial subscription
        import json as _json
        payload_bytes = len(_json.dumps(sub).encode("utf-8"))
        safe_flag = "YES" if payload_bytes < 900_000 else "NO"
        logger.info(
            "[WS_PAYLOAD][INIT] tokens=%d estimated_bytes=%d limit=1048576 safe=%s",
            len(ws_tokens), payload_bytes, safe_flag,
        )
        if payload_bytes >= 1_000_000:
            logger.error(
                "[WS_PAYLOAD][INIT] OVERSIZED — truncating to T1 only. "
                "tokens=%d bytes=%d",
                len(ws_tokens), payload_bytes,
            )
            ws_tokens = list(_cached_t1_tokens)
            sub = {"assets_ids": ws_tokens, "type": "market", "custom_feature_enabled": True}
        logger.info("[RADAR] Initial subscription: %d tokens (ws_only, %d total)", len(ws_tokens), len(_current_tokens))
        # D121-4: Update WS subscription metrics for initial subscription
        if mc:
            mc.on_ws_subscription_update(len(ws_tokens), len(_current_tokens), payload_bytes)
    else:
        sub = None
        logger.warning("[RADAR] No active tokens found, WS may receive no data")

    next_heartbeat = time.monotonic() + 10.0
    reconnect_now = False
    _live_loop_started = time.monotonic()
    _d75_hb_last = _live_loop_started
    _d75_hb_trade_base = 0
    _d75_hb_real_trade_base = 0
    _d75_hb_entropy_base = 0
    _d77_tick_last = _live_loop_started
    _d77_tick_n = 0

    # ── Run WS persistently (restarts when reconnect_now is set) ──────────
    async def _ws_runner() -> None:
        nonlocal reconnect_now, sub
        global _ws_1009_last_failure
        while True:
            now = time.monotonic()
            # D121: After 1009 FATAL error, wait 60s before rebuilding to avoid spin loop
            if _ws_1009_last_failure and (now - _ws_1009_last_failure) < 60.0:
                await asyncio.sleep(5.0)
                continue
            if reconnect_now or _current_tokens != (sub or {}).get("assets_ids", []):
                reconnect_now = False
                if not _current_tokens:
                    await asyncio.sleep(1.0)
                    continue
                # D121: Build WS token list within payload size limit
                # Priority: T1 (BTC 5m) > POL T2 > general T2. T3/T5 excluded.
                ws_tokens = _build_ws_token_list(
                    _cached_t1_tokens,
                    _cached_t2_tokens,
                    limit=_WS_TOKEN_LIMIT,
                )
                sub = {"assets_ids": ws_tokens, "type": "market", "custom_feature_enabled": True}
                # D121-3: Payload size pre-check log — before sending to WS layer
                import json as _json
                payload_bytes = len(_json.dumps(sub).encode("utf-8"))
                safe_flag = "YES" if payload_bytes < 900_000 else "NO"
                logger.info(
                    "[WS_PAYLOAD] tokens=%d estimated_bytes=%d limit=1048576 safe=%s",
                    len(ws_tokens), payload_bytes, safe_flag,
                )
                if payload_bytes >= 1_000_000:
                    logger.error(
                        "[WS_PAYLOAD] OVERSIZED — truncating to T1 only. "
                        "tokens=%d bytes=%d",
                        len(ws_tokens), payload_bytes,
                    )
                    # Emergency truncation: keep only T1 tokens
                    ws_tokens = list(_cached_t1_tokens)
                    sub = {"assets_ids": ws_tokens, "type": "market", "custom_feature_enabled": True}
                logger.info(
                    "[RADAR] WS reconnecting with %d tokens (ws_only, %d total in tier_map)...",
                    len(ws_tokens), len(_current_tokens),
                )
                # D121-4: Update WS subscription metrics
                if mc:
                    mc.on_ws_subscription_update(
                        len(ws_tokens), len(_current_tokens), payload_bytes
                    )
            _close_event.clear()
            # D123-1: Lifecycle log — every entry to run_ws_loop
            _ws_attempt_count = getattr(_ws_runner, "_attempt_count", 0) + 1
            _ws_runner._attempt_count = _ws_attempt_count  # type: ignore[attr-defined]
            first_asset = (sub.get("assets_ids") or ["(none)"])[0] if sub else "(none)"
            logger.info(
                "[WS_RUNNER][ENTER] attempt=%d assets_count=%d first_asset=%s",
                _ws_attempt_count,
                len(sub.get("assets_ids", [])) if sub else 0,
                str(first_asset)[:20],
            )
            try:
                await run_ws_loop(
                    _on_message,
                    subscribe_payload=sub,
                    on_reconnect=lambda: ew.mark_reconnect(),
                    on_connect_cb=(lambda: (mc.on_ws_connected() if mc else None)) if mc else None,
                    on_disconnect_cb=(lambda: (mc.on_ws_disconnected() if mc else None)) if mc else None,
                    close_event=_close_event,
                )
                # D121: run_ws_loop returned normally — reset 1009 flag so next attempt can proceed
                _ws_1009_last_failure = 0.0
                _ws_last_stream_msg_ts = time.monotonic()  # D123: reset on clean exit
                logger.info("[WS_RUNNER][EXIT_CLEAN] run_ws_loop returned normally")
            except asyncio.CancelledError:
                # Expected on reconnect signal — loop continues
                logger.warning("[WS_RUNNER][CANCELLED] task cancelled externally")
                raise
            except Exception as exc:
                import traceback as _tb
                tok_count = len(sub.get("assets_ids", [])) if sub else -1
                logger.error(
                    "[WS_RUNNER][EXCEPTION] %s tokens=%d\n%s",
                    exc, tok_count, _tb.format_exc()[:1000],
                )
                if "1009" in str(exc) or "message too big" in str(exc).lower():
                    _ws_1009_last_failure = time.monotonic()
                    logger.warning(
                        "[RADAR] WS 1009 received (payload still too large). "
                        "Will retry in 60s. tokens=%d",
                        tok_count,
                    )
                else:
                    logger.warning("[RADAR] WS error: %s, will retry after backoff", exc)
                await asyncio.sleep(5)

    ws_task = asyncio.create_task(_ws_runner())

    # ── Phase 5: Whale Scanner — starts once, runs independently on 300s cadence ─
    if os.getenv("PANOPTICON_WHALE"):
        from panopticon_py.hunting.whale_scanner import run_whale_scanner_loop
        whale_task = asyncio.create_task(
            run_whale_scanner_loop(db, lambda: _t2_raw_markets)
        )
        logger.info("[STARTUP] Whale scanner enabled")

    while True:
        now_loop = time.monotonic()
        # D77_LOOP_ALIVE removed D83 — stable
        if now_loop - _d77_tick_last >= 10.0:
            # D77_LOOP_TICK removed D83 — stable
            _d77_tick_last = now_loop
            _d77_tick_n += 1
        if now_loop - _d75_hb_last >= 60.0:
            trade_ticks_60s = max(0, _ws_trade_count - _d75_hb_trade_base)
            real_trade_ticks_60s = max(0, _ws_real_trade_count - _d75_hb_real_trade_base)
            entropy_fires_60s = max(0, _ws_entropy_fire_count - _d75_hb_entropy_base)
            logger.info(
                "[D75_HEARTBEAT] uptime_s=%.0f trade_ticks_60s=%d real_trade_ticks_60s=%d "
                "entropy_fires_60s=%d pol_markets_active=%d",
                now_loop - _live_loop_started,
                trade_ticks_60s,
                real_trade_ticks_60s,
                entropy_fires_60s,
                _pol_active_count,
            )
            _d75_hb_last = now_loop
            _d75_hb_trade_base = _ws_trade_count
            _d75_hb_real_trade_base = _ws_real_trade_count
            _d75_hb_entropy_base = _ws_entropy_fire_count

        # ── Heartbeat: refresh subscriptions every 10s ──────────────────────────
        if now_loop >= next_heartbeat:
            # Concurrently refresh all tiers (asyncio.gather) then re-subscribe
            new_tokens, _, _, _, _ = await _refresh_all_subscriptions(db)
            # D42: Propagate active market registry to whale_scanner so it can scan T1/T3/T5
            from panopticon_py.hunting import whale_scanner as _ws_mod
            _ws_mod.register_active_markets(_token_tier_map)
            reconnect_now = False

            # D101: T2-POL refresh — every 30 minutes scan political markets
            # via asyncio.to_thread (sync httpx called from thread, safe for event loop)
            now_ts = time.time()
            if now_ts - _last_pol_refresh >= _POL_REFRESH_INTERVAL_SEC:
                _last_pol_refresh = now_ts
                pol_tokens = await asyncio.to_thread(_sync_pol_tokens_from_watchlist, db)
                if pol_tokens:
                    existing = set(_current_tokens)
                    for t in pol_tokens:
                        if t not in existing:
                            _current_tokens.append(t)
                            existing.add(t)
                    sub = {"assets_ids": _current_tokens, "type": "market", "custom_feature_enabled": True}
                    reconnect_now = True
                    ew.mark_reconnect()

            # D103: Log T5 sports market status after POL refresh cycle
            await asyncio.to_thread(_log_t5_market_status, db)

            if new_tokens:
                existing = set(_current_tokens)
                _current_tokens = list(_current_tokens)
                for t in new_tokens:
                    if t not in existing:
                        _current_tokens.append(t)
                        existing.add(t)
                sub = {"assets_ids": _current_tokens, "type": "market", "custom_feature_enabled": True}
                reconnect_now = True
                ew.mark_reconnect()

            # Task 4: T1 window boundary trigger — refresh just before 5-min roll-over
            # This ensures new T1 market is subscribed the moment it goes live,
            # without waiting for the next 60s heartbeat cycle.
            from panopticon_py.hunting.t1_market_clock import is_t1_window_boundary
            if is_t1_window_boundary(threshold_secs=30):
                logger.info(
                    "[L1_TIER1] Window boundary detected — triggering clock-based T1 refresh early"
                )
                # Reset the 60s rate limit by resetting _last_tier1_refresh
                _last_tier1_refresh = 0.0
                tier1_extra = _refresh_tier1_tokens(db)
                for t in tier1_extra:
                    if t not in existing:
                        _current_tokens.append(t)
                        existing.add(t)
                if tier1_extra:
                    sub = {"assets_ids": _current_tokens, "type": "market", "custom_feature_enabled": True}
                    reconnect_now = True
                # Notify MetricsCollector of T1 window rollover
                try:
                    from panopticon_py.hunting.t1_market_clock import get_current_t1_window
                    window_ts = get_current_t1_window(corrected=True)
                    window_end = window_ts + 300
                    secs_left = max(0, window_end - int(time.time()))
                    mc.on_t1_window_rollover(
                        window_start=window_ts,
                        window_end=window_end,
                        secs_remaining=float(secs_left),
                    )
                except Exception:
                    pass

            # Data API poll for taker addresses
            _poll_data_api_for_takers(_current_tokens, db)

            state = ew.state_dict()

            # P2 DIAG: periodic L1 WebSocket counters (every 60s)
            now = time.monotonic()
            if now - _last_ws_diag_log_ts >= _WS_DIAG_LOG_INTERVAL_SEC:
                # D78_60S_BLOCK removed D83 — stable
                # ── BATCH: flush accumulated wallet_obs and kyle samples ──────────
                db.flush_wallet_obs_buffer()
                db.flush_kyle_buffer()
                # D98: close stale orders (orders with no new fills for 30s)
                closed = close_stale_orders(db)
                if closed > 0:
                    logger.info("[D98][ORDER_CLEANUP] closed %d stale orders", closed)
                # P3 DIAG: log actual z-score even when no entropy fire
                # D75: minute-level z-score distribution stats
                z_min = min(_entropy_z_samples) if _entropy_z_samples else None
                z_p50 = _pctl(_entropy_z_samples, 0.50)
                z_p90 = _pctl(_entropy_z_samples, 0.90)
                z_max = max(_entropy_z_samples) if _entropy_z_samples else None
                z_min_str = f"{z_min:.3f}" if z_min is not None else "None"
                z_p50_str = f"{z_p50:.3f}" if z_p50 is not None else "None"
                z_p90_str = f"{z_p90:.3f}" if z_p90 is not None else "None"
                z_max_str = f"{z_max:.3f}" if z_max is not None else "None"
                threshold_str = f"{get_z_threshold():.3f}"
                # D83: D75_ENTROPY_GATE reverted to logger.info (log handler confirmed working)
                logger.info(
                    "[D75_ENTROPY_GATE] event_type_60s={last_trade_price:%d,book:%d,price_change:%d,other:%d} "
                    "gate_60s={eval:%d,locked:%d,history_not_ready:%d,z_ready:%d,z_below_threshold:%d,fired:%d} "
                    "z_dist_60s={min:%s,p50:%s,p90:%s,max:%s,threshold:%s}",
                    _evt_count["last_trade_price"], _evt_count["book"], _evt_count["price_change"], _evt_count["other"],
                    _entropy_eval_total, _entropy_locked_count, _entropy_history_not_ready_count,
                    _entropy_z_ready_count, _entropy_z_below_threshold_count, _ws_entropy_fire_count,
                    z_min_str, z_p50_str, z_p90_str, z_max_str, threshold_str,
                )
                _evt_count = {"last_trade_price": 0, "book": 0, "price_change": 0, "other": 0}
                _entropy_eval_total = 0
                _entropy_locked_count = 0
                _entropy_history_not_ready_count = 0
                _entropy_z_ready_count = 0
                _entropy_z_below_threshold_count = 0
                _entropy_z_samples = []
                _last_ws_diag_log_ts = now
                # ── MetricsCollector: collect + persist (every 60s) ─────────────────
                # D77: Collect and persist metrics every 60s
                mc = _mc()
                if mc is not None:
                    mc.sync_series_from_db(db)  # D37 FIX: fill series stats from DB
                    mc.sync_consensus_from_db(db)  # D48: fill wallet/consensus stats from DB
                    mc.persist_db(db)

            logger.info(
                "[RADAR %s] Buffer Events: %s, Trigger Locked: %s, H Hist: %s",
                os.getpid(),
                len(recent),
                state.get("trigger_locked"),
                state.get("h_hist"),
            )
            next_heartbeat = time.monotonic() + 10.0

        # Yield to WS runner task briefly — don't block the heartbeat loop
        await asyncio.sleep(0.1)


async def _main_async(args: argparse.Namespace, signal_queue: asyncio.Queue | None = None) -> int:
    import os as _os
    _os.makedirs("data", exist_ok=True)
    db = ShadowDB()
    db.bootstrap()

    # ── Module-level DB reference (used by sync helpers that can't take db param) ─
    global _radar_db
    _radar_db = db

    # ── MetricsCollector: get singleton + sync baseline ────────────────────────
    mc = _mc()
    if mc is not None:
        _sync_metrics_baseline(db, mc)

    # ── D58b: Batch-fill polymarket_link_map on startup ────────────────────
    import os as _os
    db_path = _os.path.join(_os.path.dirname(__file__), "..", "..", "data", "panopticon.db")
    db_path = _os.path.abspath(db_path)
    try:
        filled = _batch_fill_link_map(db_path, lookback_days=30)
        logger.info("[D65] batch_fill: %d link_map rows added", filled)
    except Exception as e:
        logger.warning("[D58b] batch_fill failed: %s", e)

    # ── D70 Q1: Immediately resolve BTC 5m windows on startup ──────────────
    try:
        initial_rows = await resolve_btc_5m_windows(db, lookahead=3)
        if initial_rows:
            total = db.conn.execute("SELECT COUNT(*) FROM polymarket_link_map").fetchone()[0]
            logger.info("[D70] Initial BTC 5m resolve: +%d rows, total=%d", initial_rows, total)
    except Exception as e:
        logger.warning("[D70] Initial BTC 5m resolve failed: %s", e)

    # ── D71b: Slug Consistency Audit ─────────────────────────────────────────
    # Compare slugs in link_map (from resolve_btc_5m_windows) vs
    # t1_market_clock.get_current_t1_window() (source-of-truth reference).
    # t1_market_clock.py is read-only in D71 — do NOT modify it.
    try:
        from panopticon_py.hunting.t1_market_clock import get_current_t1_window

        # Step 1: All BTC slugs currently in link_map
        link_slugs = [
            row[0] for row in db.conn.execute(
                "SELECT slug FROM polymarket_link_map WHERE slug LIKE 'btc-updown-5m-%' AND slug IS NOT NULL"
            ).fetchall()
        ]
        logger.info(
            "[D71_SLUG_AUDIT] link_map BTC slugs: %s",
            link_slugs,
        )

        # Step 2: get_current_t1_window() UTC timestamp
        t1_window_ts = get_current_t1_window()
        logger.info(
            "[D71_SLUG_AUDIT] get_current_t1_window() = %d",
            t1_window_ts,
        )

        # Step 3: Expected slug from t1_market_clock
        expected_slug = f"btc-updown-5m-{t1_window_ts}"
        slug_in_link = expected_slug in link_slugs

        if slug_in_link:
            logger.info("[D71_SLUG_OK] expected_slug=%s found in link_map", expected_slug)
        else:
            logger.warning(
                "[D71_SLUG_MISMATCH] expected_slug=%s NOT in link_map; "
                "link_slugs=%s — escalating to Architect",
                expected_slug, link_slugs,
            )
    except Exception as e:
        logger.warning("[D71_SLUG_AUDIT] audit failed: %s", e)

    # ── D109: POL immediate startup scan (no 30-min wait for first POL data) ──
    # Note: uses time.time() (Unix timestamp) to match _last_pol_refresh comparison
    # in the heartbeat loop at L2688 (time.time() vs _last_pol_refresh diff).
    try:
        from panopticon_py.hunting.pol_monitor import sync_scan_pol_markets
        global _last_pol_refresh  # allow assignment to module-level var
        _initial_pol_count = sync_scan_pol_markets(db, max_pages=3)
        _last_pol_refresh = time.time()  # matches heartbeat loop L2688 units
        logger.info("[POL][D109] startup scan complete count=%d", _initial_pol_count)
    except Exception as exc:
        logger.warning("[POL][D109] startup scan failed: %s", exc)
    # Even if POL scan fails, startup continues — POL is supplementary, not critical

    # D103: Log T5 sports market status at startup
    _log_t5_market_status(db)

    # ── Start 5s JSON write loop ───────────────────────────────────────────────
    if mc is not None:
        asyncio.create_task(
            _metrics_json_loop(mc, db, path="data/rvf_live_snapshot.json"),
            name="metrics-json-loop",
        )

    ew = EntropyWindow()
    if args.synthetic:
        await _synthetic_ticks(ew, db, float(args.duration_sec))
    else:
        # Start BTC 5m resolve loop as independent background task
        btc_resolve_task = asyncio.create_task(
            _btc5m_resolve_loop(db),
            name="btc5m-resolve-loop",
        )
        try:
            await _live_ticks(ew, db, signal_queue=signal_queue)
        except asyncio.CancelledError:
            pass
        finally:
            btc_resolve_task.cancel()
            try:
                await asyncio.wait_for(btc_resolve_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
    print(json.dumps({"ok": True, "entropy_state": ew.state_dict()}))
    return 0


def main() -> int:
    load_repo_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # D51: Singleton enforcement
    from panopticon_py.utils.process_guard import acquire_singleton
    PROCESS_VERSION = "v1.1.50-D131"   # ← AGENT: bump on every change  # D131: +on_real_trade_tick hook + mc.on_real_trade_tick() calls in _ws_runner
    acquire_singleton("radar", PROCESS_VERSION)
    ap = argparse.ArgumentParser(description="Hunting entropy radar (shadow hits only)")
    ap.add_argument("--duration-sec", type=float, default=15.0)
    ap.add_argument("--synthetic", action="store_true", help="Do not connect to real WS")
    args = ap.parse_args()
    return asyncio.run(_main_async(args, signal_queue=None))


if __name__ == "__main__":
    raise SystemExit(main())