"""
panopticon_py/hunting/t1_market_clock.py

T1 Market Clock — deterministic BTC/ETH/SOL 5-min Up-or-Down token discovery.

T1 markets follow a strict 5-minute rolling schedule:
  slug = "{asset}-updown-5m-{window_start_unix}"
  window_start_unix = floor(now / 300) * 300  (300 seconds = 5 minutes)

This module computes the current and upcoming T1 market slugs mathematically,
then resolves their clobTokenIds via targeted Gamma API calls.
No pagination, no keyword search, no Gamma listing needed.

Confirmed slug patterns (from diagnostic):
  btc-updown-5m-{unix_ts}  → "Bitcoin Up or Down - April 24, 2:05AM-2:10AM ET"
  eth-updown-5m-{unix_ts}  → "Ethereum Up or Down - April 24, 2:05AM-2:10AM ET"
  sol-updown-5m-{unix_ts}  → "Solana Up or Down - April 24, 2:05AM-2:10AM ET"
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Confirmed T1 asset prefixes (from diagnostic run)
T1_ASSET_PREFIXES = [
    "btc-updown-5m",
    "eth-updown-5m",
    "sol-updown-5m",
]

# How many windows ahead to pre-fetch (current + N future windows)
# D30: preload at least 5 rolling windows (~25 minutes coverage) to avoid
# subscription gaps for 5-minute BTC/ETH/SOL event rollovers.
T1_PREFETCH_WINDOWS = 5

# ── NTP Time Correction ───────────────────────────────────────────────────────
# Module-level NTP state. Updated at startup and once per hour.
_ntp_offset_seconds: float = 0.0
_last_ntp_sync: float = 0.0

# Known anchor from embed analysis (user-verified 2026-04-24 HKT):
#   btc-updown-5m-1777018200 = HKT 2026-04-24 16:10:00+08:00
_KNOWN_ANCHOR_TS = 1777018200
_KNOWN_ANCHOR_HKT = "2026-04-24 16:10:00+08:00"


def sync_ntp_offset(ntp_server: str = "pool.ntp.org", timeout: float = 3.0) -> float:
    """
    Query NTP server to measure local clock offset vs network time.
    Returns offset in seconds (positive = local clock is behind).
    Stores result in _ntp_offset_seconds module variable.
    On failure: returns 0.0, logs WARNING, does NOT crash.
    """
    global _ntp_offset_seconds, _last_ntp_sync
    try:
        import ntplib
        c = ntplib.NTPClient()
        response = c.request(ntp_server, version=3, timeout=timeout)
        _ntp_offset_seconds = response.offset
        _last_ntp_sync = _time.time()
        logger.info(
            "[T1_CLOCK][NTP] offset=%.3fs server=%s (local clock %s)",
            _ntp_offset_seconds, ntp_server,
            "ahead" if _ntp_offset_seconds < 0 else "behind",
        )
        return _ntp_offset_seconds
    except Exception as e:
        logger.warning("[T1_CLOCK][NTP] sync failed: %s — using local clock", e)
        _ntp_offset_seconds = 0.0
        _last_ntp_sync = _time.time()
        return 0.0


def get_corrected_unix_time() -> float:
    """
    Returns current Unix time corrected for NTP offset.
    This is the ONLY function that should be used for T1 window computation.
    Falls back to time.time() if NTP failed (offset=0.0).
    """
    return _time.time() + _ntp_offset_seconds


def get_current_t1_window(corrected: bool = True) -> int:
    """
    Return Unix timestamp of the current 5-min window start (UTC-aligned).
    Uses NTP-corrected time by default.
    corrected=False for testing only.
    """
    t = get_corrected_unix_time() if corrected else _time.time()
    return (int(t) // 300) * 300


def validate_clock_against_anchor(anchor_ts: int = _KNOWN_ANCHOR_TS) -> bool:
    """
    Validates that our clock formula produces the correct window for the
    known anchor (1777018200 = HKT 16:10). This is a read-only formula check
    — it does NOT use current time.

    Returns True if anchor_ts % 300 == 0 and the HKT conversion matches.
    """
    if anchor_ts % 300 != 0:
        logger.error("[T1_CLOCK][ANCHOR_FAIL] anchor_ts=%d not 300-aligned", anchor_ts)
        return False
    hkt = timezone(timedelta(hours=8))
    anchor_dt = datetime.fromtimestamp(anchor_ts, tz=hkt)
    expected_dt = datetime.fromisoformat(_KNOWN_ANCHOR_HKT)
    if anchor_dt != expected_dt:
        logger.error(
            "[T1_CLOCK][ANCHOR_FAIL] formula mismatch: got=%s expected=%s",
            anchor_dt.isoformat(), expected_dt.isoformat(),
        )
        return False
    logger.info(
        "[T1_CLOCK][ANCHOR_OK] formula validated: anchor %d = %s HKT",
        anchor_ts, anchor_dt.strftime("%H:%M"),
    )
    return True


def compute_t1_slugs(window_ts: int | None = None, prefetch: int = T1_PREFETCH_WINDOWS) -> list[str]:
    """
    Compute T1 slugs for current window + N future windows, all assets.

    Args:
        window_ts: Base timestamp to compute from (default: current 5-min boundary).
                   Used for testing with fixed timestamps.
        prefetch: Number of future windows to include (default: 3).

    Returns:
        List of slugs like ["btc-updown-5m-1777010700", "eth-updown-5m-1777010700", ...]
    """
    base = window_ts if window_ts is not None else get_current_t1_window()
    slugs = []
    for offset in range(prefetch):
        ts = base + (offset * 300)
        for prefix in T1_ASSET_PREFIXES:
            slugs.append(f"{prefix}-{ts}")
    return slugs


def _extract_token_ids(m: dict) -> list[str]:
    """
    Safely extract clobTokenIds from a Gamma market dict.
    Handles: JSON string, list, nested list, None.
    """
    raw = m.get("clobTokenIds") or m.get("clob_token_ids") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            return []
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        if isinstance(item, list):
            result.extend(str(t) for t in item if t)
        elif item:
            result.append(str(item))
    return result


async def resolve_t1_tokens(
    slugs: list[str],
    http_client: Optional[httpx.AsyncClient] = None,
) -> tuple[list[str], dict[str, str]]:
    """
    Resolve T1 slugs to clobTokenIds via Gamma API.

    Args:
        slugs: List of T1 slugs to resolve (e.g. ["btc-updown-5m-1777010700", ...])
        http_client: Optional shared HTTP client (avoids creating new connections).

    Returns:
        (token_ids: list[str], token_to_slug: dict[str, str])
    """
    should_close = http_client is None
    if http_client is None:
        http_client = httpx.AsyncClient(timeout=8.0)

    token_ids: list[str] = []
    token_to_slug: dict[str, str] = {}

    try:
        tasks = [
            http_client.get(GAMMA_MARKETS_URL, params={"slug": slug})
            for slug in slugs
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for slug, resp in zip(slugs, responses):
            if isinstance(resp, Exception):
                logger.debug("[T1_CLOCK] slug=%s fetch error: %s", slug, resp)
                continue
            try:
                data = resp.json()
                markets = data if isinstance(data, list) else [data]
                for m in markets:
                    if not m or not isinstance(m, dict):
                        continue
                    tids = _extract_token_ids(m)
                    for tid in tids:
                        token_ids.append(tid)
                        token_to_slug[tid] = slug
                    if tids:
                        logger.info(
                            "[T1_CLOCK] resolved slug=%s → %d token(s)",
                            slug, len(tids),
                        )
            except Exception as e:
                logger.debug("[T1_CLOCK] slug=%s parse error: %s", slug, e)

    finally:
        if should_close:
            await http_client.aclose()

    # Deduplicate while preserving order (first-seen wins)
    seen: set[str] = set()
    deduped: list[str] = []
    for tid in token_ids:
        if tid not in seen:
            seen.add(tid)
            deduped.append(tid)

    logger.info(
        "[T1_CLOCK] total_slugs_tried=%d tokens_resolved=%d",
        len(slugs), len(deduped),
    )
    return deduped, token_to_slug


async def refresh_t1_tokens_via_clock() -> tuple[list[str], dict[str, str]]:
    """
    Main entry point for run_radar.py.

    Computes T1 slugs for current + next windows, resolves clobTokenIds via Gamma,
    returns (token_ids, token_to_slug).

    Also handles:
    - NTP hourly sync
    - Window rollover detection + logging
    - Late-start warning if subscribing mid-window
    """
    global _last_ntp_sync, _last_logged_window_ts

    # ── NTP sync (once per hour) ──────────────────────────────────────
    if _time.time() - _last_ntp_sync > 3600:
        sync_ntp_offset()
        _last_ntp_sync = _time.time()

    # ── Window rollover detection ────────────────────────────────────
    current_window = get_current_t1_window()
    if current_window != _last_logged_window_ts:
        hkt = timezone(timedelta(hours=8))
        window_start_hkt = datetime.fromtimestamp(current_window, tz=hkt)
        window_end_hkt = datetime.fromtimestamp(current_window + 300, tz=hkt)
        secs_into = int(get_corrected_unix_time()) - current_window
        logger.info(
            "[T1_WINDOW_ROLLOVER] NEW WINDOW ts=%d HKT=%s–%s secs_into_window=%ds ntp_offset=%.3fs",
            current_window,
            window_start_hkt.strftime("%H:%M:%S"),
            window_end_hkt.strftime("%H:%M:%S"),
            secs_into,
            _ntp_offset_seconds,
        )
        _last_logged_window_ts = current_window

    # ── Late-start warning ──────────────────────────────────────────
    secs_into = int(get_corrected_unix_time()) - current_window
    if secs_into > 30:
        logger.warning(
            "[T1_WINDOW_LATE_SUB] subscribing %ds into window ts=%d — "
            "first %ds of flow may be missed",
            secs_into, current_window, secs_into,
        )

    slugs = compute_t1_slugs()
    logger.info("[T1_CLOCK] computed %d slugs (current+%d windows, %d assets)",
                len(slugs), T1_PREFETCH_WINDOWS - 1, len(T1_ASSET_PREFIXES))
    return await resolve_t1_tokens(slugs)


_last_logged_window_ts: int = 0


def is_t1_window_boundary(threshold_secs: int = 60) -> bool:
    """
    Return True if we are within `threshold_secs` of a new 5-min window starting.
    Uses NTP-corrected time.

    This is used to trigger an early T1 refresh just before market roll-over,
    ensuring no subscription gap when a market expires and a new one begins.
    """
    secs_into_window = int(get_corrected_unix_time()) % 300
    return secs_into_window >= (300 - threshold_secs)


def get_window_expiry_seconds() -> int:
    """Seconds remaining until the current 5-min window expires (NTP-corrected)."""
    secs = 300 - (int(get_corrected_unix_time()) % 300)
    # Keep return range in [0, 299] for boundary stability.
    return 0 if secs == 300 else secs