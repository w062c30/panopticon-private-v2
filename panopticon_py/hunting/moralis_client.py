"""Moralis REST with hard pagination caps (graph explosion guard)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from panopticon_py.rate_limit_governor import RateLimitGovernor


def moralis_base_url() -> str:
    try:
        p = Path(__file__).resolve().parents[2] / "config" / "api_capability_registry.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(
            data.get("apis", {})
            .get("moralis", {})
            .get("base_urls", {})
            .get("evm_api", "https://deep-index.moralis.io/api/v2.2")
        ).rstrip("/")
    except Exception:
        return "https://deep-index.moralis.io/api/v2.2"


def fetch_wallet_erc20_transfers_capped(
    address: str,
    *,
    governor: RateLimitGovernor | None = None,
    page_limit: int | None = None,
    row_hard_cap: int | None = None,
    per_page: int = 50,
    timeout_sec: float = 20.0,
) -> list[dict[str, Any]]:
    """
    Fetch ERC20 transfers with **at most** ``page_limit`` cursor pages and ``row_hard_cap`` rows.
    """
    key = os.getenv("MORALIS_API_KEY", "").strip()
    if not key:
        return []
    max_pages = int(page_limit if page_limit is not None else os.getenv("HUNT_MORALIS_MAX_PAGES", "3"))
    cap = int(row_hard_cap if row_hard_cap is not None else os.getenv("HUNT_MORALIS_ROW_CAP", "150"))
    base = os.getenv("MORALIS_EVM_API_BASE", moralis_base_url()).rstrip("/")
    addr = address.lower()[:42]
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    pages = 0
    while pages < max_pages and len(out) < cap:
        if governor and not governor.allow("moralis_cu", 2.0):
            break
        q = f"{base}/{addr}/erc20/transfers?chain=polygon&limit={per_page}"
        if cursor:
            q += f"&cursor={urllib.parse.quote(cursor, safe='')}"
        req = urllib.request.Request(
            q,
            headers={"X-API-Key": key, "Accept": "application/json", "User-Agent": "panopticon-hunting/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            break
        chunk = []
        cursor = None
        if isinstance(body, list):
            chunk = [x for x in body if isinstance(x, dict)]
        elif isinstance(body, dict):
            res = body.get("result")
            if isinstance(res, list):
                chunk = [x for x in res if isinstance(x, dict)]
            c = body.get("cursor")
            cursor = str(c) if c else None
        out.extend(chunk)
        pages += 1
        if not chunk:
            break
        if not cursor:
            break
        if len(out) >= cap:
            break
    return out[:cap]


def _parse_iso_ts_ms(value: str) -> float:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.timestamp() * 1000.0
    except ValueError:
        return 0.0


def map_erc20_transfers_to_history_rows(rows: list[dict[str, Any]], wallet_address: str) -> list[dict[str, Any]]:
    """
    將 Moralis ERC20 transfer 資料轉成 discovery scrubber 所需欄位。
    註：transfer 不是成交資料，這裡僅作 fallback proxy，用於行為風險初篩。
    """
    wallet = wallet_address.lower()
    out: list[dict[str, Any]] = []
    for r in rows:
        fa = str(r.get("from_address") or r.get("from") or "").lower()
        ta = str(r.get("to_address") or r.get("to") or "").lower()
        if fa != wallet and ta != wallet:
            continue
        side = "SELL" if fa == wallet else "BUY"
        val = r.get("value_formatted") or r.get("value") or 0
        try:
            notional = abs(float(val))
        except (TypeError, ValueError):
            notional = 0.0
        ts_raw = str(r.get("block_timestamp") or r.get("timestamp") or "")
        ts_ms = _parse_iso_ts_ms(ts_raw) if ts_raw else 0.0
        out.append(
            {
                "side": side,
                "notional_usd": notional,
                "balance_before_usd": 0.0,
                "ts_ms": ts_ms,
                "market_id": str(r.get("token_address") or ""),
                "source": "moralis",
            }
        )
    return out
