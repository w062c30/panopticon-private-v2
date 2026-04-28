from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import requests

from panopticon_py.market_data.clob_series import registry_clob_base


def _clob_base() -> str:
    return os.getenv("POLYMARKET_CLOB_BASE", registry_clob_base()).rstrip("/")


def fetch_book(token_id: str, timeout_sec: float = 15.0) -> dict[str, Any] | None:
    url = f"{_clob_base()}/book?token_id={urllib.parse.quote(token_id, safe='')}"
    return _http_json_get(url, timeout_sec)


def fetch_trades(token_id: str, *, limit: int = 80, timeout_sec: float = 8.0) -> list[dict[str, Any]]:
    """Fetch recent trades for an outcome token. Tries common CLOB paths; returns [].
    Each URL attempt is limited to timeout_sec; the overall fetch is capped at 15s."""
    tid = urllib.parse.quote(token_id, safe="")
    candidates: list[str] = []
    tpl = os.getenv("CLOB_TRADES_URL_TEMPLATE")
    if tpl:
        candidates.append(tpl.format(base=_clob_base(), token_id=token_id, limit=limit))
    candidates.extend(
        [
            f"{_clob_base()}/data/trades?asset_id={tid}&limit={limit}",
            f"{_clob_base()}/trades?asset_id={tid}&limit={limit}",
        ]
    )
    # Global 15s ceiling across all candidates to prevent unbounded blocking
    import time
    deadline = time.monotonic() + 15.0
    for url in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        body = _http_json_get(url, min(timeout_sec, remaining))
        if body is None:
            continue
        if isinstance(body, list):
            return [x for x in body if isinstance(x, dict)]
        if isinstance(body, dict):
            for key in ("trades", "data", "result"):
                arr = body.get(key)
                if isinstance(arr, list):
                    return [x for x in arr if isinstance(x, dict)]
    return []


def _http_json_get(url: str, timeout_sec: float) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "panopticon-ingestion/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return None


AMM_SPREAD_THRESHOLD = 0.85  # spread > this → pure AMM (no CLOB trades at all)


def has_recent_clob_trades(
    token_id: str,
    lookback_secs: int = 300,
    timeout_sec: float = 3.0,
) -> bool:
    """
    Return True if this token_id had any CLOB trades in the last
    `lookback_secs` seconds.

    D69 Q2 Ruling: Volume presence = CLOB market. Overrides spread-based
    AMM detection for hybrid AMM+CLOB markets (e.g. BTC 5m has outer AMM
    quotes but internal CLOB trades at mid-market prices).

    Method: GET https://clob.polymarket.com/trades?token_id=...&limit=1
    If returned trade has timestamp within lookback → CLOB active.
    If no trade returned or trade is stale → possibly pure AMM (use spread).

    Args:
        token_id: CLOB token_id to check.
        lookback_secs: Seconds to look back for recent trades. Default 300 (5 min).
        timeout_sec: HTTP timeout.

    Returns:
        True = CLOB or hybrid market (real trades exist recently).
        False = possibly pure AMM (fall back to spread check).
    """
    try:
        r = requests.get(
            "https://clob.polymarket.com/trades",
            params={"token_id": token_id, "limit": 1},
            timeout=timeout_sec,
        )
        if not r.ok:
            return False
        trades = r.json()
        if not trades:
            return False
        t = trades[0] if isinstance(trades, list) else {}
        if not t:
            return False
        # Check timestamp — try common CLOB timestamp keys
        for k in ("timestamp", "matchTime", "createdAt", "time"):
            ts = t.get(k)
            if ts is None:
                continue
            try:
                trade_ts = float(ts)
                # Normalize: if looks like milliseconds (> 1e12), convert to seconds
                if trade_ts > 1e12:
                    trade_ts /= 1000
                return (time.time() - trade_ts) <= lookback_secs
            except (ValueError, TypeError):
                continue
        # Trade exists but no timestamp we could parse → optimistically True
        return True
    except Exception:
        return False  # network error → fall back to spread check


def is_amm_market(best_bid: float | None, best_ask: float | None) -> bool:
    """
    Return True if bid/ask spread indicates AMM pricing (no real CLOB trades).
    D67 Q1 Ruling: AMM markets have fixed synthetic spreads (bid=0.01/ask=0.99).
    Detection: spread = (best_ask - best_bid) > 0.85.

    NOTE (D69 Q2): This is now a FALLBACK check only — for markets where
    has_recent_clob_trades returned False (no CLOB activity detected).
    For markets with confirmed CLOB trades, spread check is skipped.
    """
    if best_bid is None or best_ask is None:
        return False
    return (best_ask - best_bid) > AMM_SPREAD_THRESHOLD


def fetch_best_ask(token_id: str, timeout_sec: float = 3.0) -> float | None:
    """
    Return the best ask (lowest price) from CLOB order book.
    Returns None if no asks available — caller should NO_TRADE.

    D67 Q1 Ruling: skip AMM markets (spread > 0.85) — NO real CLOB entry.
    D69 Q2 Ruling: Volume-based override — if has_recent_clob_trades=True,
    this is a hybrid AMM+CLOB market → allow entry at ask price.

    Logic:
      1. Fetch book bids/asks.
      2. If no asks → return None (no price).
      3. Compute spread = ask - bid.
      4. If spread > AMM_SPREAD_THRESHOLD:
         a. First check: has_recent_clob_trades(token_id)?
            → True: hybrid market → log and return ask price.
            → False: pure AMM → return None (no entry).
      5. Otherwise → return ask price (normal CLOB market).
    """
    book = fetch_book(token_id, timeout_sec=timeout_sec)
    if book is None:
        return None
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not asks:
        return None
    try:
        best_bid = float(bids[0]["price"]) if bids else None
        best_ask_price = float(asks[0]["price"])
    except (KeyError, ValueError, TypeError):
        return None

    import logging
    logger = logging.getLogger(__name__)

    if is_amm_market(best_bid, best_ask_price):
        spread = (best_ask_price - best_bid) if best_bid else 0.0
        # D69 Q2: Before blocking, check if real CLOB trades exist
        if has_recent_clob_trades(token_id, lookback_secs=300):
            # Hybrid AMM+CLOB — volume confirmed, allow entry
            logger.info(
                "[ENTRY_PRICE] Hybrid AMM+CLOB token=%s spread=%.3f "
                "but recent CLOB trades confirmed — using ask",
                token_id[:16], spread
            )
            return best_ask_price
        # Pure AMM — no real CLOB trades
        logger.info(
            "[ENTRY_PRICE] Pure AMM token=%s spread=%.3f NO_TRADE",
            token_id[:16], spread
        )
        return None
    return best_ask_price


def fetch_last_trade_price(token_id: str, timeout_sec: float = 3.0) -> float | None:
    """
    Return the last trade price for a token from CLOB /book.
    Returns None if unavailable.
    """
    book = fetch_book(token_id, timeout_sec=timeout_sec)
    if book is None:
        return None
    last = book.get("last_trade_price")
    if last is None or last == "":
        return None
    try:
        return float(last)
    except (ValueError, TypeError):
        return None
