from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any


def registry_clob_base() -> str:
    """Default Polymarket CLOB origin from ``config/api_capability_registry.json``."""
    return _registry_clob_base()


def _registry_clob_base() -> str:
    try:
        import pathlib

        p = pathlib.Path(__file__).resolve().parents[2] / "config" / "api_capability_registry.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(
            data.get("apis", {})
            .get("polymarket", {})
            .get("base_urls", {})
            .get("clob", "https://clob.polymarket.com")
        )
    except Exception:
        return "https://clob.polymarket.com"


def fetch_mid_series_stub(market_id: str, length: int = 48) -> list[float]:
    """Deterministic pseudo-mid series for offline / tests (not a market price)."""
    h = hashlib.sha256(market_id.encode()).digest()
    out: list[float] = []
    for i in range(length):
        b = h[i % len(h)]
        out.append(0.45 + (b / 255.0) * 0.1 + (i % 7) * 0.001)
    return out


def fetch_mid_series_clob(token_id: str, *, interval_sec: int = 300) -> list[float]:
    """
    Best-effort CLOB history fetch. Polymarket REST evolves; on failure returns [].
    Override URL with CLOB_PRICES_HISTORY_URL template containing {token_id}.
    """
    tpl = os.getenv("CLOB_PRICES_HISTORY_URL")
    if tpl:
        url = tpl.format(token_id=token_id)
    else:
        base = os.getenv("POLYMARKET_CLOB_BASE", registry_clob_base()).rstrip("/")
        url = f"{base}/prices-history?interval=max&fidelity={interval_sec}&market={token_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "panopticon/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(body, list):
        return [_mid_from_point(x) for x in body if _mid_from_point(x) is not None]
    if isinstance(body, dict) and "history" in body:
        hist = body["history"]
        if isinstance(hist, list):
            return [_mid_from_point(x) for x in hist if _mid_from_point(x) is not None]
    return []


def _mid_from_point(x: Any) -> float | None:
    if not isinstance(x, dict):
        return None
    if "p" in x and isinstance(x["p"], (int, float)):
        return float(x["p"])
    if "price" in x and isinstance(x["price"], (int, float)):
        return float(x["price"])
    return None


def fetch_settlement_price(token_id: str, timeout_sec: float = 5.0) -> float | None:
    """
    Return the final settlement price for a closed market token.
    D64 Q2 Ruling: exit price = CLOB /prices-history last price (rounded to 0/1).

    Returns None if prices-history is unavailable.
    Does NOT fall back to estimation — exit_price=null is intentional when data missing.
    """
    import time
    deadline = time.monotonic() + timeout_sec

    # Step A: Try Gamma API to resolve condition_id from token_id
    gamma_base = os.getenv("GAMMA_API_BASE", "https://gamma-api.polymarket.com").rstrip("/")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        import urllib.parse
        query = urllib.parse.urlencode({"clob_token_ids": token_id})
        gamma_url = f"{gamma_base}/markets?{query}"
        req = urllib.request.Request(gamma_url, headers={"User-Agent": "panopticon/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=min(timeout_sec, remaining)) as resp:
            markets = json.loads(resp.read().decode("utf-8"))
        if markets and isinstance(markets, list) and len(markets) > 0:
            condition_id = markets[0].get("conditionId") or token_id
        else:
            condition_id = token_id
    except Exception:
        condition_id = token_id

    # Step B: Fetch price history
    base = os.getenv("POLYMARKET_CLOB_BASE", registry_clob_base()).rstrip("/")
    hist_url = f"{base}/prices-history?interval=max&fidelity=1&market={urllib.parse.quote(condition_id, safe='')}"
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        req = urllib.request.Request(hist_url, headers={"User-Agent": "panopticon/1.0"})
        with urllib.request.urlopen(req, timeout=min(timeout_sec, remaining)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    # Step C: Extract last price from history
    history = []
    if isinstance(body, list):
        history = body
    elif isinstance(body, dict):
        history = body.get("history", [])

    if not history:
        return None

    last_point = history[-1]
    last_price = _mid_from_point(last_point)
    if last_price is None:
        return None

    # Step D: Round to 0 or 1 for clearly resolved markets
    if last_price >= 0.95:
        return 1.0
    elif last_price <= 0.05:
        return 0.0
    else:
        return last_price
