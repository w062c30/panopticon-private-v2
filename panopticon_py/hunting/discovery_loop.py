"""Autonomous wallet discovery daemon: macro harvest -> scrub -> sybil merge -> DB hydration."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import uuid4

import httpx

from panopticon_py.db import ShadowDB
from panopticon_py.hunting.entity_linker import sybil_group_wallets, trace_funding_roots
from panopticon_py.hunting.fingerprint_scrubber import (
    WalletTradeSample,
    fetch_wallet_history,
    scrub_wallet_for_discovery,
)
from panopticon_py.hunting.moralis_client import (
    fetch_wallet_erc20_transfers_capped,
    map_erc20_transfers_to_history_rows,
)
from panopticon_py.load_env import load_repo_env
from panopticon_py.time_utils import utc_now_rfc3339_ms

logger = logging.getLogger(__name__)


def _utc() -> str:
    return utc_now_rfc3339_ms()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class DiscoveryCandidate:
    wallet_address: str
    all_time_pnl: float
    win_rate: float
    markets_7d: int
    discovery_source: str


@dataclass(frozen=True)
class GammaWalletResult:
    """
    Wrapper returned by fetch_active_wallet_candidates_gamma_public.
    Retains the raw Gamma payload for Token-ID extraction (Track A).
    """

    raw_payload: Any
    candidates: list[DiscoveryCandidate]


Fetcher = Callable[[str], Awaitable[list[dict[str, Any]]]]


@dataclass
class MoralisPressureStats:
    calls: int = 0
    estimated_cu: int = 0
    errors_429: int = 0
    transport_errors: int = 0


@dataclass
class DiscoveryRuntimeStats:
    moralis: MoralisPressureStats = field(default_factory=MoralisPressureStats)
    provider_used: str = "mock"
    gamma_candidates_fetched: int = 0
    tier1_added_this_round: int = 0


class LeaderboardRateLimitGovernor:
    """防止過度呼叫 data-api.polymarket.com。"""

    def __init__(self, min_interval_ms: float = 200.0):
        self._last_call_at: float = 0.0
        self._lock = asyncio.Lock()
        self._min_interval_sec = min_interval_ms / 1000.0

    async def guard(self) -> None:
        async with self._lock:
            elapsed = time.monotonic() - self._last_call_at
            if elapsed < self._min_interval_sec:
                await asyncio.sleep(self._min_interval_sec - elapsed)
        self._last_call_at = time.monotonic()


async def _fetch_recent_active_traders_from_gamma(
    *,
    timeout_sec: float = 15.0,
    max_wallets: int = 50,
) -> list[DiscoveryCandidate]:
    """
    Tier-3 fallback for Track B.
    Scrapes recent Gamma markets to find traders who traded in last 7 days,
    regardless of PnL size. Used when both leaderboard tiers come up sparse.
    """
    base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").rstrip("/")
    url = f"{base}/markets"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "panopticon-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            if int(getattr(resp, "status", 200)) >= 400:
                return []
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        for k in ("result", "results", "data", "markets"):
            v = payload.get(k)
            if isinstance(v, list):
                rows = [x for x in v if isinstance(x, dict)]
                break

    # Accumulate unique wallets from recent markets (field names vary by API version)
    seen: dict[str, float] = {}
    for row in rows:
        for field in ("trader", "wallet", "address", "user"):
            w = str(row.get(field) or "").strip().lower()
            if w.startswith("0x") and len(w) >= 42:
                pnl = float(row.get("pnl") or row.get("realized_pnl") or row.get("profit_usd") or 0)
                if w not in seen:
                    seen[w] = 0.0
                seen[w] = max(seen[w], pnl)
        if len(seen) >= max_wallets:
            break

    return [
        DiscoveryCandidate(
            wallet_address=addr,
            all_time_pnl=pnl,
            win_rate=0.0,
            markets_7d=0,
            discovery_source="TRACK_B_GAMMA_FALLBACK",
        )
        for addr, pnl in seen.items()
    ]


async def fetch_top_political_whales(
    category: str = "POLITICS",
    time_period: str = "ALL",
    limit: int = 100,
) -> list[DiscoveryCandidate]:
    """
    Track B：宏觀捕鯨。
    呼叫 data-api.polymarket.com/v1/leaderboard（公開，無需認證）。

    Progressive relaxation strategy (3 tiers):
      Tier 1 — pnl > 5000 AND vol > 0  (strict, top whales)
      Tier 2 — pnl > 500 AND vol > 0   (relaxed, mid-tier winners)
      Tier 3 — recent market winners from Gamma (last-resort fallback)

    Returns as soon as ≥10 Tier-1 or ≥20 combined candidates are found.
    """
    governor = LeaderboardRateLimitGovernor()
    base = os.getenv("LEADERBOARD_API_BASE", "https://data-api.polymarket.com").rstrip("/")
    cfg_limit = _env_int("LEADERBOARD_LIMIT", 100)
    effective_limit = min(limit, cfg_limit, 50)

    # ── Tier 1: Strict leaderboard filter ─────────────────────────────────────
    candidates: list[DiscoveryCandidate] = []
    tier1_count = 0

    url = f"{base}/v1/leaderboard"
    params = {"category": category, "timePeriod": time_period, "limit": effective_limit}
    await governor.guard()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                logger.warning("leaderboard_http_%d", resp.status_code)
            else:
                entries = resp.json()
                for entry in entries:
                    pnl = float(entry.get("pnl") or 0)
                    vol = float(entry.get("vol") or 0)
                    wallet = entry.get("proxyWallet") or entry.get("address") or ""
                    if not isinstance(wallet, str) or not wallet.startswith("0x"):
                        continue
                    if pnl > 5000 and vol > 0:
                        candidates.append(
                            DiscoveryCandidate(
                                wallet_address=wallet.lower(),
                                all_time_pnl=pnl,
                                win_rate=0.0,
                                markets_7d=0,
                                discovery_source="TRACK_B_LEADERBOARD",
                            )
                        )
                        tier1_count += 1
                logger.info(
                    "track_b_tier1_found",
                    extra={"tier1_count": tier1_count, "category": category, "time_period": time_period},
                )
    except Exception as exc:
        logger.warning("track_b_leaderboard_failed", extra={"error": str(exc)})

    # ── Tier 2: Relaxed threshold if Tier 1 is sparse ─────────────────────────
    if tier1_count < 10:
        url2 = f"{base}/v1/leaderboard"
        params2 = {"category": category, "timePeriod": time_period, "limit": effective_limit}
        await governor.guard()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url2, params=params2)
                if resp.status_code == 200:
                    entries = resp.json()
                    added = 0
                    for entry in entries:
                        pnl = float(entry.get("pnl") or 0)
                        vol = float(entry.get("vol") or 0)
                        wallet = entry.get("proxyWallet") or entry.get("address") or ""
                        if not isinstance(wallet, str) or not wallet.startswith("0x"):
                            continue
                        addr = wallet.lower()
                        if pnl > 500 and vol > 0 and not any(c.wallet_address == addr for c in candidates):
                            candidates.append(
                                DiscoveryCandidate(
                                    wallet_address=addr,
                                    all_time_pnl=pnl,
                                    win_rate=0.0,
                                    markets_7d=0,
                                    discovery_source="TRACK_B_LEADERBOARD_RELAXED",
                                )
                            )
                            added += 1
                    logger.info(
                        "track_b_tier2_relaxed",
                        extra={"added": added, "total": len(candidates)},
                    )
        except Exception as exc:
            logger.warning("track_b_tier2_relaxed_failed", extra={"error": str(exc)})

    # ── Tier 3: Fallback — recent active traders from Gamma markets ───────────
    if len(candidates) < 20:
        try:
            recent = await _fetch_recent_active_traders_from_gamma()
            existing = {c.wallet_address for c in candidates}
            added = 0
            for w in recent:
                if w.wallet_address not in existing:
                    candidates.append(w)
                    existing.add(w.wallet_address)
                    added += 1
            if added:
                logger.info("track_b_tier3_fallback_gamma", extra={"added": added, "total": len(candidates)})
        except Exception as exc:
            logger.warning("track_b_tier3_fallback_failed", extra={"error": str(exc)})

    logger.info("track_b_final_candidates", extra={"count": len(candidates)})
    return candidates


async def fetch_active_wallet_candidates(
    *,
    days: int = 7,
    min_markets: int = 10,
    min_win_rate: float = 0.6,
    limit: int = 500,
) -> list[DiscoveryCandidate]:
    """
    Mock provider for Gamma/Moralis integration.
    實際串接時替換這層，不改 discovery 主流程。
    """
    out: list[DiscoveryCandidate] = []
    for i in range(limit):
        wr = 0.55 + (i % 50) / 100
        mk = 8 + (i % 20)
        if wr < min_win_rate or mk <= min_markets:
            continue
        out.append(
            DiscoveryCandidate(
                wallet_address=f"0x{i:040x}",
                all_time_pnl=1000.0 + i * 5.0,
                win_rate=min(0.95, wr),
                markets_7d=mk,
                discovery_source=f"macro_harvest_{days}d",
            )
        )
    return out[:limit]


def _extract_wallet_candidates_from_gamma_payload(
    payload: Any,
    *,
    min_markets: int,
    min_win_rate: float,
    limit: int,
) -> list[DiscoveryCandidate]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        for k in ("result", "results", "data", "markets"):
            v = payload.get(k)
            if isinstance(v, list):
                rows = [x for x in v if isinstance(x, dict)]
                break

    by_wallet: dict[str, dict[str, float | int]] = {}
    for row in rows:
        wallet = (
            str(row.get("wallet_address") or row.get("wallet") or row.get("trader") or row.get("address") or "")
            .lower()
            .strip()
        )
        if not (wallet.startswith("0x") and len(wallet) >= 42):
            continue
        entry = by_wallet.setdefault(
            wallet[:42],
            {"markets": set(), "wins": 0.0, "trades": 0.0, "pnl": 0.0},
        )
        market_id = row.get("market_id") or row.get("market") or row.get("conditionId") or row.get("condition_id")
        if isinstance(market_id, (str, int, float)):
            casted = str(market_id)
            if casted:
                entry["markets"].add(casted)  # type: ignore[attr-defined]
        realized = row.get("realized_pnl_usd") or row.get("pnl") or row.get("profit_usd") or 0.0
        try:
            entry["pnl"] = float(entry["pnl"]) + float(realized)
        except (TypeError, ValueError):
            pass
        wr = row.get("win_rate")
        if wr is not None:
            try:
                entry["wins"] = float(entry["wins"]) + float(wr)
                entry["trades"] = float(entry["trades"]) + 1.0
            except (TypeError, ValueError):
                pass

    out: list[DiscoveryCandidate] = []
    for wallet, agg in by_wallet.items():
        markets_7d = len(agg["markets"])  # type: ignore[arg-type]
        avg_wr = float(agg["wins"]) / max(1.0, float(agg["trades"]))
        if markets_7d <= min_markets or avg_wr < min_win_rate:
            continue
        out.append(
            DiscoveryCandidate(
                wallet_address=wallet,
                all_time_pnl=float(agg["pnl"]),
                win_rate=min(1.0, max(0.0, avg_wr)),
                markets_7d=markets_7d,
                discovery_source="gamma_public",
            )
        )
    out.sort(key=lambda x: (x.win_rate, x.all_time_pnl, x.markets_7d), reverse=True)
    return out[:limit]


async def fetch_active_wallet_candidates_gamma_public(
    *,
    days: int = 7,
    min_markets: int = 10,
    min_win_rate: float = 0.6,
    limit: int = 500,
    timeout_sec: float = 15.0,
) -> GammaWalletResult:
    base = os.getenv("GAMMA_PUBLIC_API_BASE", "https://gamma-api.polymarket.com").rstrip("/")
    path = os.getenv("GAMMA_PUBLIC_MARKETS_PATH", "/markets")
    url = f"{base}{path}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "panopticon-discovery/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            status = int(getattr(resp, "status", 200))
            if status >= 400:
                raise RuntimeError(f"gamma_public_http_{status}")
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (429, 500, 502, 503, 504):
            raise RuntimeError(f"gamma_retryable_http_{exc.code}") from exc
        raise RuntimeError(f"gamma_http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("gamma_transport_or_decode_error") from exc
    candidates = _extract_wallet_candidates_from_gamma_payload(
        payload,
        min_markets=min_markets,
        min_win_rate=min_win_rate,
        limit=limit,
    )
    return GammaWalletResult(raw_payload=payload, candidates=candidates)


def _extract_token_ids_from_gamma_payload(payload: Any) -> list[str]:
    """
    從 Gamma markets payload 中萃取出所有 clobTokenIds。
    每個市場含 YES/NO 兩個 token，回傳去重後的 token_id 清單。
    """
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        for k in ("result", "results", "data", "markets"):
            v = payload.get(k)
            if isinstance(v, list):
                rows = [x for x in v if isinstance(x, dict)]
                break

    token_ids: set[str] = set()
    for row in rows:
        raw = row.get("clobTokenIds") or row.get("clob_token_ids") or []
        # Gamma API returns clobTokenIds as a JSON string (e.g. '["token1","token2"]')
        # not a native list, so also handle the string case
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                raw = []
        if isinstance(raw, list):
            for tid in raw:
                # Accept any non-empty string or numeric ID (Gamma returns decimal integers as strings)
                if isinstance(tid, str) and tid.strip():
                    token_ids.add(tid.strip())
                elif isinstance(tid, (int, float)) and tid not in (None, 0):
                    token_ids.add(str(tid))
    return sorted(token_ids)


class DataApiRateLimitGovernor:
    """
    Rate limiter for Data API /trades endpoint.
    Rate limit: 200 req / 10s (sliding window).
    """

    def __init__(self) -> None:
        self._times: list[float] = []
        self._window_sec = 10.0
        self._max_calls = 180  # conservative: 180 calls in 10s window

    async def guard(self) -> None:
        now = time.monotonic()
        # Remove expired entries
        cutoff = now - self._window_sec
        self._times = [t for t in self._times if t > cutoff]
        if len(self._times) >= self._max_calls:
            sleep_time = self._times[0] + self._window_sec - now + 0.1
            if sleep_time > 0:
                logger.info("data_api_rate_limit_gov_sleeping", extra={"sleep_sec": sleep_time})
                await asyncio.sleep(sleep_time)
                now = time.monotonic()
                self._times = [t for t in self._times if t > now - self._window_sec]
        self._times.append(now)


async def _fetch_takers_via_clob_trades(
    token_ids: list[str],
    limit_per_token: int = 100,
) -> list[DiscoveryCandidate]:
    """
    Track A（微觀獵犬）：
    使用公開的 Data API /trades endpoint（無需認證）抓取 taker 地址。
    Rate Limit: 200 req / 10s，使用 DataApiRateLimitGovernor 控制。

    [Invariant 1.1] 雙軌資料源：此函式實現 Track A，捕捉微觀即時 taker。
    """
    seen: dict[str, float] = {}
    governor = DataApiRateLimitGovernor()
    data_api_base = "https://data-api.polymarket.com"

    for tid in token_ids[:20]:  # 最多 20 個 token
        await governor.guard()
        try:
            url = f"{data_api_base}/trades?asset_id={tid}&limit={limit_per_token}"
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/json", "User-Agent": "panopticon-discovery/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15.0) as resp:
                status = int(getattr(resp, "status", 200))
                if status >= 400:
                    continue
                raw = resp.read().decode("utf-8")
                try:
                    trades = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
        except Exception:
            continue

        if not isinstance(trades, list):
            continue
        for trade in trades:
            # Data API returns proxyWallet as the taker
            taker = str(trade.get("proxyWallet") or trade.get("taker_address") or "").strip()
            if not taker.startswith("0x") or len(taker) < 42:
                continue
            addr = taker[:42].lower()
            pnl_val = float(trade.get("pnl") or trade.get("realized_pnl") or 0.0)
            if addr not in seen:
                seen[addr] = 0.0
            seen[addr] += pnl_val

    logger.info("track_a_takers_fetched", extra={"unique_takers": len(seen), "tokens_checked": min(20, len(token_ids))})

    return [
        DiscoveryCandidate(
            wallet_address=addr,
            all_time_pnl=pnl,
            win_rate=0.0,
            markets_7d=0,
            discovery_source="TRACK_A_CLOB_TAKER",
        )
        for addr, pnl in seen.items()
    ]


async def mock_wallet_history_fetcher(wallet: str) -> list[dict[str, Any]]:
    seed = int(wallet[-4:], 16) if wallet.startswith("0x") else 1
    random.seed(seed)
    balance = 10_000.0
    rows: list[dict[str, Any]] = []
    for i in range(40):
        notional = random.uniform(50.0, 900.0)
        # 少數樣本故意觸發 Kelly 違規，以驗證過濾能力
        if i == 3 and seed % 17 == 0:
            notional = max(notional, balance * 0.55)
        rows.append(
            {
                "side": "BUY" if random.random() > 0.35 else "SELL",
                "notional_usd": notional,
                "balance_before_usd": balance,
                "ts_ms": 1_700_000_000_000 + i * 10_000 + seed,
                "market_id": f"mkt_{seed % 12}",
            }
        )
        balance = max(500.0, balance + random.uniform(-200.0, 300.0))
    return rows


def _history_from_observations(db: ShadowDB, wallet: str, *, limit: int = 300) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    # Flush pending buffer so recent observations are visible to the query
    db.flush_wallet_obs_buffer()
    rows = db.fetch_recent_wallet_observations(wallet.lower(), limit=limit)
    for r in rows:
        if r.get("obs_type") != "clob_trade":
            continue
        # fetch_recent_wallet_observations already parses payload_json into a dict;
        # payload is directly the trade object (e.g. {"side": "BUY", "size": 13.26, ...})
        # NOT {"trade": {"side": "BUY", "size": ...}}
        payload = r.get("payload", {}) if isinstance(r.get("payload"), dict) else {}
        side = str(payload.get("side") or "BUY")
        size = payload.get("size") or payload.get("matched_amount") or payload.get("amount") or 0
        try:
            notional = abs(float(size))
        except (TypeError, ValueError):
            notional = 0.0
        out.append(
            {
                "side": side,
                "notional_usd": notional,
                "balance_before_usd": 0.0,
                "ts_ms": 0.0,
                "market_id": r.get("market_id"),
                "source": "observation",
                "last_seen_ts_utc": r.get("ingest_ts_utc"),
            }
        )
    return out


async def make_hybrid_history_fetcher(db: ShadowDB) -> Fetcher:
    min_obs = _env_int("DISCOVERY_HISTORY_MIN_OBS", 20)
    moralis_timeout = _env_float("DISCOVERY_HTTP_TIMEOUT_SEC", 15.0)

    async def _fetch(wallet: str) -> list[dict[str, Any]]:
        obs_rows = _history_from_observations(db, wallet)
        if len(obs_rows) >= min_obs:
            return obs_rows
        moralis_rows = fetch_wallet_erc20_transfers_capped(wallet, timeout_sec=moralis_timeout)
        moralis_hist = map_erc20_transfers_to_history_rows(moralis_rows, wallet)
        return obs_rows + moralis_hist

    return _fetch


async def make_hybrid_history_fetcher_with_stats(
    db: ShadowDB,
    stats: DiscoveryRuntimeStats,
) -> Fetcher:
    min_obs = _env_int("DISCOVERY_HISTORY_MIN_OBS", 20)
    moralis_timeout = _env_float("DISCOVERY_HTTP_TIMEOUT_SEC", 15.0)

    async def _fetch(wallet: str) -> list[dict[str, Any]]:
        obs_rows = _history_from_observations(db, wallet)
        if len(obs_rows) >= min_obs:
            return obs_rows
        stats.moralis.calls += 1
        try:
            moralis_rows = fetch_wallet_erc20_transfers_capped(wallet, timeout_sec=moralis_timeout)
            stats.moralis.estimated_cu += 2
        except Exception as exc:  # noqa: BLE001
            if "429" in str(exc):
                stats.moralis.errors_429 += 1
            else:
                stats.moralis.transport_errors += 1
            moralis_rows = []
        moralis_hist = map_erc20_transfers_to_history_rows(moralis_rows, wallet)
        return obs_rows + moralis_hist

    return _fetch


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    retries: int = 4,
    base_backoff_sec: float = 0.8,
) -> Any:
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001
            if attempt == retries:
                raise
            delay = base_backoff_sec * (2 ** (attempt - 1)) + random.uniform(0, 0.2)
            logger.warning(
                "discovery_retry",
                extra={
                    "attempt": attempt,
                    "retries": retries,
                    "delay_sec": round(delay, 3),
                    "error": str(exc),
                },
            )
            await asyncio.sleep(delay)


def _label_weight(primary_tag: str) -> float:
    return {
        "ALGO_SLICING": 0.75,
        "LONG_TERM_HOLDER": 0.55,
        "SMART_MONEY_QUANT": 0.65,
    }.get(primary_tag, 0.5)


def bayesian_trust_score(
    *,
    prev_score: float | None,
    win_rate: float,
    sample_size: int,
    primary_tag: str,
) -> float:
    prior = (prev_score / 100.0) if prev_score is not None else 0.5
    evidence = max(0.0, min(1.0, win_rate)) * 0.7 + _label_weight(primary_tag) * 0.3
    k = min(1.0, sample_size / 100.0)
    posterior = (1.0 - k) * prior + k * evidence
    return round(max(0.0, min(1.0, posterior)) * 100.0, 3)


async def run_discovery_cycle(
    db: ShadowDB,
    *,
    history_fetcher: Fetcher,
    cycle_id: str,
    loop_policy: dict[str, Any] | None = None,
    runtime_stats: DiscoveryRuntimeStats | None = None,
) -> dict[str, Any]:
    logger.info("discovery_cycle_start", extra={"cycle_id": cycle_id})
    provider = os.getenv("DISCOVERY_PROVIDER", "mock").strip().lower()
    timeout = _env_float("DISCOVERY_HTTP_TIMEOUT_SEC", 15.0)
    provider_errors: dict[str, int] = {}
    if provider == "gamma_public":
        try:
            candidates = await with_retry(
                lambda: fetch_active_wallet_candidates_gamma_public(
                    days=7,
                    min_markets=10,
                    min_win_rate=0.6,
                    limit=500,
                    timeout_sec=timeout,
                )
            )
        except Exception as exc:  # noqa: BLE001
            k = str(exc) or "gamma_unknown"
            provider_errors[k] = provider_errors.get(k, 0) + 1
            logger.warning("gamma_provider_failed_fallback_to_mock", extra={"cycle_id": cycle_id, "error": k})
            candidates = await with_retry(
                lambda: fetch_active_wallet_candidates(days=7, min_markets=10, min_win_rate=0.6, limit=500)
            )
    else:
        candidates = await with_retry(
            lambda: fetch_active_wallet_candidates(days=7, min_markets=10, min_win_rate=0.6, limit=500)
        )
    if runtime_stats:
        runtime_stats.provider_used = provider
        # dual_track also uses Track A (Gamma public API) as its primary candidate source
        runtime_stats.gamma_candidates_fetched = len(candidates) if provider in ("gamma_public", "dual_track") else 0

    survivors: list[dict[str, Any]] = []
    dropped = 0
    hist_obs_only = 0
    hist_moralis_only = 0
    hist_hybrid = 0
    for c in candidates:
        history_rows = await with_retry(lambda wallet=c.wallet_address: history_fetcher(wallet))
        async def _history_provider(_wallet: str, rows: list[dict[str, Any]] = history_rows) -> list[dict[str, Any]]:
            return rows

        history: list[WalletTradeSample] = await fetch_wallet_history(c.wallet_address, _history_provider)
        scrub = scrub_wallet_for_discovery(
            c.wallet_address,
            history,
            candidate_pnl=c.all_time_pnl,
            candidate_source=c.discovery_source,
        )
        no_history_with_signal = "no_history_observed_with_source_signal" in scrub.reasons
        if scrub.drop_tag is not None and not no_history_with_signal:
            dropped += 1
            logger.info(
                "discovery_wallet_dropped",
                extra={
                    "cycle_id": cycle_id,
                    "wallet": c.wallet_address,
                    "drop_tag": scrub.drop_tag,
                    "reasons": ",".join(scrub.reasons),
                },
            )
            continue
        sources = {str(x.get("source") or "") for x in history_rows}
        if not sources:
            # No history rows — quality depends on whether we have a candidate signal
            source_quality = "no_history_" + ("candidate_signal" if no_history_with_signal else "no_signal")
            # These wallets don't fall into any history bucket
        elif sources == {"observation"}:
            source_quality = "observation_only"
            hist_obs_only += 1
        elif sources == {"moralis"}:
            source_quality = "moralis_only"
            hist_moralis_only += 1
        else:
            source_quality = "hybrid"
            hist_hybrid += 1
        last_seen = None
        for x in history_rows:
            v = x.get("last_seen_ts_utc")
            if isinstance(v, str):
                if (last_seen is None) or (v > last_seen):
                    last_seen = v
        roots = trace_funding_roots(c.wallet_address).get("roots", [])
        db.upsert_wallet_funding_roots(c.wallet_address.lower()[:42], roots, _utc())
        survivors.append(
            {
                "wallet_address": c.wallet_address.lower(),
                "all_time_pnl": c.all_time_pnl,
                "win_rate": c.win_rate,
                "discovery_source": c.discovery_source,
                "funding_roots": roots,
                "trade_ts_ms": [h.ts_ms for h in history],
                "sample_size": len(history),
                "source_quality": source_quality,
                "history_sample_size": len(history_rows),
                "last_seen_ts_utc": last_seen or _utc(),
            }
        )

    groups = sybil_group_wallets(survivors)
    entity_rows = 0
    wallet_rows = 0
    ts_now = _utc()

    for entity_id, members in groups.items():
        grouped = [x for x in survivors if x["wallet_address"] in members]
        if not grouped:
            continue
        avg_wr = sum(float(x["win_rate"]) for x in grouped) / len(grouped)
        sample_size = sum(int(x["sample_size"]) for x in grouped)
        prev = db.fetch_discovered_entity(entity_id)
        primary_tag = "ALGO_SLICING" if avg_wr >= 0.7 else "LONG_TERM_HOLDER"
        trust = bayesian_trust_score(
            prev_score=float(prev["trust_score"]) if prev else None,
            win_rate=avg_wr,
            sample_size=sample_size,
            primary_tag=primary_tag,
        )
        db.upsert_discovered_entity(
            {
                "entity_id": entity_id,
                "trust_score": trust,
                "primary_tag": primary_tag,
                "sample_size": sample_size,
                "last_updated_at": ts_now,
            }
        )
        entity_rows += 1
        for w in grouped:
            db.upsert_tracked_wallet(
                {
                    "wallet_address": w["wallet_address"],
                    "entity_id": entity_id,
                    "all_time_pnl": w["all_time_pnl"],
                    "win_rate": w["win_rate"],
                    "discovery_source": w["discovery_source"],
                    "source_quality": w["source_quality"],
                    "history_sample_size": w["history_sample_size"],
                    "last_seen_ts_utc": w["last_seen_ts_utc"],
                    "last_updated_at": ts_now,
                }
            )
            wallet_rows += 1

    summary = {
        "cycle_id": cycle_id,
        "fetched_candidates": len(candidates),
        "dropped": dropped,
        "survivors": len(survivors),
        "entities_upserted": entity_rows,
        "wallets_upserted": wallet_rows,
        "provider": provider,
        "gamma_candidates_count": len(candidates) if provider == "gamma_public" else 0,
        "history_from_observation": hist_obs_only,
        "history_from_moralis": hist_moralis_only,
        "history_hybrid_count": hist_hybrid,
        "provider_errors": provider_errors,
    }
    if loop_policy:
        summary["loop_policy"] = dict(loop_policy)
    if runtime_stats:
        summary["moralis_pressure"] = {
            "calls": runtime_stats.moralis.calls,
            "estimated_cu": runtime_stats.moralis.estimated_cu,
            "errors_429": runtime_stats.moralis.errors_429,
            "transport_errors": runtime_stats.moralis.transport_errors,
        }
    db.append_discovery_audit(
        {
            "audit_id": str(uuid4()),
            "actor": "wallet_discovery_daemon",
            "action": "DISCOVERY_CYCLE_SUMMARY",
            "before_json": None,
            "after_json": summary,
            "reason": "macro_sweep_scrub_sybil_hydration",
            "created_ts_utc": ts_now,
        }
    )
    logger.info("discovery_cycle_done", extra=summary)
    if runtime_stats:
        report = {
            "provider_used": runtime_stats.provider_used,
            "gamma_candidates_fetched": runtime_stats.gamma_candidates_fetched,
            "tier1_added_this_round": int(runtime_stats.tier1_added_this_round),
            "moralis_api_calls": int(runtime_stats.moralis.calls),
            "moralis_pressure": {
                "estimated_cu": int(runtime_stats.moralis.estimated_cu),
                "errors_429": int(runtime_stats.moralis.errors_429),
                "transport_errors": int(runtime_stats.moralis.transport_errors),
            },
            "cycle_id": cycle_id,
        }
        logger.info("discovery_cycle_runtime_report %s", json.dumps(report, ensure_ascii=False))
    return summary


async def run_dual_track_discovery_cycle(
    db: ShadowDB,
    *,
    history_fetcher: Fetcher,
    cycle_id: str,
    loop_policy: dict[str, Any] | None = None,
    runtime_stats: DiscoveryRuntimeStats | None = None,
) -> dict[str, Any]:
    """
    雙軌協調器：
    - Track B（宏觀捕鯨）：fetch_top_political_whales() → Leaderboard Top 100 活躍贏家
    - Track A（微觀獵犬）：Gamma clobTokenIds → CLOB REST trades → taker 地址
    兩軌合併 → 共同 scrubber → entity linking → DB upsert
    """
    logger.info("dual_track_cycle_start", extra={"cycle_id": cycle_id})

    # ── Track B：Leaderboard 宏觀捕鯨（自帶三層 progressive relaxation）──────────
    candidates_b: list[DiscoveryCandidate] = []
    try:
        candidates_b = await fetch_top_political_whales(
            category=os.getenv("LEADERBOARD_CATEGORY", "POLITICS"),
            time_period=os.getenv("LEADERBOARD_TIME_PERIOD", "ALL"),
            limit=_env_int("LEADERBOARD_LIMIT", 100),
        )
    except Exception as exc:
        logger.warning("track_b_leaderboard_failed", extra={"cycle_id": cycle_id, "error": str(exc)})

    # ── Track A：Gamma clobTokenIds + CLOB REST trades 微觀獵犬 ─────────────
    candidates_a: list[DiscoveryCandidate] = []
    try:
        gamma_result = await with_retry(
            lambda: fetch_active_wallet_candidates_gamma_public(
                days=7,
                min_markets=10,
                min_win_rate=0.6,
                limit=500,
                timeout_sec=_env_float("DISCOVERY_HTTP_TIMEOUT_SEC", 15.0),
            )
        )
        # 萃取 clobTokenIds（從原始 Gamma API payload 而非已轉換的候選對象）
        token_ids = _extract_token_ids_from_gamma_payload(gamma_result.raw_payload)
        if token_ids:
            candidates_a = await _fetch_takers_via_clob_trades(token_ids)
        if not candidates_a:
            # Primary (CLOB takers) empty — still add Gamma's own candidates as track_a
            logger.info("track_a_clob_empty_using_gamma_candidates", extra={"gamma_candidates": len(gamma_result.candidates)})
            candidates_a = gamma_result.candidates
    except Exception as exc:
        logger.warning("track_a_gamma_or_clob_failed", extra={"cycle_id": cycle_id, "error": str(exc)})
        # Track A fully failed — fall back to pure Gamma public candidates directly
        try:
            gamma_result = await fetch_active_wallet_candidates_gamma_public(
                days=7,
                min_markets=10,
                min_win_rate=0.0,  # relax win_rate so we get any active trader
                limit=500,
                timeout_sec=_env_float("DISCOVERY_HTTP_TIMEOUT_SEC", 15.0),
            )
            candidates_a = gamma_result.candidates
            logger.info("track_a_emergency_fallback_gamma", extra={"candidates": len(candidates_a)})
        except Exception as exc2:
            logger.warning("track_a_emergency_fallback_also_failed", extra={"error": str(exc2)})
            candidates_a = []

    # ── 合併 + 去重 ────────────────────────────────────────────────────────────
    by_address: dict[str, DiscoveryCandidate] = {}
    for c in candidates_a:
        if c.wallet_address not in by_address:
            by_address[c.wallet_address] = c
    for c in candidates_b:
        if c.wallet_address not in by_address:
            by_address[c.wallet_address] = c

    all_candidates = list(by_address.values())

    # ── 共同 scrubber + entity linking + DB upsert（復用現有邏輯）─────────────
    survivors: list[dict[str, Any]] = []
    for c in all_candidates:
        history_rows = await with_retry(lambda wallet=c.wallet_address: history_fetcher(wallet))
        async def _history_provider(_wallet: str, rows: list[dict[str, Any]] = history_rows) -> list[dict[str, Any]]:
            return rows

        history: list[WalletTradeSample] = await fetch_wallet_history(c.wallet_address, _history_provider)
        scrub = scrub_wallet_for_discovery(
            c.wallet_address,
            history,
            candidate_pnl=c.all_time_pnl,
            candidate_source=c.discovery_source,
        )
        # Wallets with no history but credible source signal go to uncertain bucket
        # (not dropped), so they accumulate history over cycles.
        no_history_with_signal = "no_history_observed_with_source_signal" in scrub.reasons
        if scrub.drop_tag is not None and not no_history_with_signal:
            logger.info(
                "discovery_wallet_dropped",
                extra={
                    "cycle_id": cycle_id,
                    "wallet": c.wallet_address,
                    "drop_tag": scrub.drop_tag,
                    "reasons": ",".join(scrub.reasons),
                },
            )
            continue
        sources = {str(x.get("source") or "") for x in history_rows}
        if not sources:
            # No history rows — quality depends on whether we have a candidate signal
            source_quality = "no_history_" + ("candidate_signal" if no_history_with_signal else "no_signal")
        else:
            source_quality = (
                "observation_only" if sources == {"observation"}
                else ("moralis_only" if sources == {"moralis"} else "hybrid")
            )
        last_seen = None
        for x in history_rows:
            v = x.get("last_seen_ts_utc")
            if isinstance(v, str) and (last_seen is None or v > last_seen):
                last_seen = v
        roots = trace_funding_roots(c.wallet_address).get("roots", [])
        db.upsert_wallet_funding_roots(c.wallet_address.lower()[:42], roots, _utc())
        survivors.append(
            {
                "wallet_address": c.wallet_address.lower(),
                "all_time_pnl": c.all_time_pnl,
                "win_rate": c.win_rate,
                "discovery_source": c.discovery_source,
                "funding_roots": roots,
                "trade_ts_ms": [h.ts_ms for h in history],
                "sample_size": len(history),
                "source_quality": source_quality,
                "history_sample_size": len(history_rows),
                "last_seen_ts_utc": last_seen or _utc(),
            }
        )

    groups = sybil_group_wallets(survivors)
    entity_rows = 0
    wallet_rows = 0
    ts_now = _utc()

    for entity_id, members in groups.items():
        grouped = [x for x in survivors if x["wallet_address"] in members]
        if not grouped:
            continue
        avg_wr = sum(float(x["win_rate"]) for x in grouped) / len(grouped)
        sample_size = sum(int(x["sample_size"]) for x in grouped)
        prev = db.fetch_discovered_entity(entity_id)
        primary_tag = "ALGO_SLICING" if avg_wr >= 0.7 else "LONG_TERM_HOLDER"
        trust = bayesian_trust_score(
            prev_score=float(prev["trust_score"]) if prev else None,
            win_rate=avg_wr,
            sample_size=sample_size,
            primary_tag=primary_tag,
        )
        db.upsert_discovered_entity(
            {
                "entity_id": entity_id,
                "trust_score": trust,
                "primary_tag": primary_tag,
                "sample_size": sample_size,
                "last_updated_at": ts_now,
            }
        )
        entity_rows += 1
        for w in grouped:
            db.upsert_tracked_wallet(
                {
                    "wallet_address": w["wallet_address"],
                    "entity_id": entity_id,
                    "all_time_pnl": w["all_time_pnl"],
                    "win_rate": w["win_rate"],
                    "discovery_source": w["discovery_source"],
                    "source_quality": w["source_quality"],
                    "history_sample_size": w["history_sample_size"],
                    "last_seen_ts_utc": w["last_seen_ts_utc"],
                    "last_updated_at": ts_now,
                }
            )
            wallet_rows += 1

    summary = {
        "cycle_id": cycle_id,
        "track_a_count": len(candidates_a),
        "track_b_count": len(candidates_b),
        "total_candidates": len(all_candidates),
        "survivors": len(survivors),
        "entities_upserted": entity_rows,
        "wallets_upserted": wallet_rows,
    }

    db.append_discovery_audit(
        {
            "audit_id": str(uuid4()),
            "actor": "wallet_discovery_daemon",
            "action": "DUAL_TRACK_CYCLE_SUMMARY",
            "before_json": None,
            "after_json": summary,
            "reason": "dual_track_scrub_sybil_hydration",
            "created_ts_utc": ts_now,
        }
    )
    logger.info(
        "[DISCOVERY SUMMARY] Track A: %d, Track B: %d, Passed Scrubber: %d",
        len(candidates_a),
        len(candidates_b),
        len(survivors),
    )
    logger.info("dual_track_cycle_done", extra=summary)
    if runtime_stats:
        # Track A (Gamma) is the primary candidate source for dual_track
        runtime_stats.gamma_candidates_fetched = len(candidates_a)
        report = {
            "provider_used": "dual_track",
            "track_a_count": len(candidates_a),
            "track_b_count": len(candidates_b),
            "tier1_added_this_round": int(runtime_stats.tier1_added_this_round),
            "moralis_api_calls": int(runtime_stats.moralis.calls),
            "moralis_pressure": {
                "estimated_cu": int(runtime_stats.moralis.estimated_cu),
                "errors_429": int(runtime_stats.moralis.errors_429),
                "transport_errors": int(runtime_stats.moralis.transport_errors),
            },
            "cycle_id": cycle_id,
        }
        logger.info("discovery_cycle_runtime_report %s", json.dumps(report, ensure_ascii=False))
    return summary


def _resolve_discovery_interval_hours(
    *,
    elapsed_hours: float,
    tier1_count: int,
    cold_start_hours: float,
    cold_start_interval_hours: float,
    relaxed_interval_hours: float,
    tier1_threshold: int,
) -> tuple[float, str]:
    if tier1_count >= tier1_threshold:
        return relaxed_interval_hours, "tier1_threshold"
    if elapsed_hours >= cold_start_hours:
        return relaxed_interval_hours, "time_window_elapsed"
    return cold_start_interval_hours, "cold_start_window"


async def discovery_main_loop(
    *,
    interval_hours: float = 12.0,
    run_once: bool = False,
) -> None:
    if os.getenv("LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes"}:
        raise RuntimeError("LIVE_TRADING must be false in shadow hydration mode")

    db = ShadowDB()
    db.bootstrap()
    runtime_stats = DiscoveryRuntimeStats()
    history_fetcher = await make_hybrid_history_fetcher_with_stats(db, runtime_stats)
    loop_started = datetime.now(timezone.utc)
    cold_start_interval_hours = _env_float("DISCOVERY_COLD_START_INTERVAL_HOURS", 2.0)
    cold_start_window_hours = _env_float("DISCOVERY_COLD_START_WINDOW_HOURS", 48.0)
    relaxed_interval_hours = _env_float("DISCOVERY_RELAXED_INTERVAL_HOURS", 6.0)
    tier1_threshold = _env_int("DISCOVERY_TIER1_THRESHOLD", 100)

    while True:
        cycle_id = str(uuid4())
        try:
            runtime_stats = DiscoveryRuntimeStats()
            history_fetcher = await make_hybrid_history_fetcher_with_stats(db, runtime_stats)
            tier1_before = db.count_tier1_entities()
            elapsed_hours = (datetime.now(timezone.utc) - loop_started).total_seconds() / 3600.0
            next_interval_hours, relax_reason = _resolve_discovery_interval_hours(
                elapsed_hours=elapsed_hours,
                tier1_count=tier1_before,
                cold_start_hours=cold_start_window_hours,
                cold_start_interval_hours=cold_start_interval_hours,
                relaxed_interval_hours=relaxed_interval_hours,
                tier1_threshold=tier1_threshold,
            )
            provider = os.getenv("DISCOVERY_PROVIDER", "dual_track").strip().lower()
            if provider == "dual_track":
                await run_dual_track_discovery_cycle(
                    db,
                    history_fetcher=history_fetcher,
                    cycle_id=cycle_id,
                    loop_policy={
                        "elapsed_hours": round(elapsed_hours, 3),
                        "tier1_count_before": tier1_before,
                        "next_interval_hours": next_interval_hours,
                        "relax_reason": relax_reason,
                    },
                    runtime_stats=runtime_stats,
                )
            else:
                await run_discovery_cycle(
                    db,
                    history_fetcher=history_fetcher,
                    cycle_id=cycle_id,
                    loop_policy={
                        "elapsed_hours": round(elapsed_hours, 3),
                        "tier1_count_before": tier1_before,
                        "next_interval_hours": next_interval_hours,
                        "relax_reason": relax_reason,
                    },
                    runtime_stats=runtime_stats,
                )
            tier1_after = db.count_tier1_entities()
            new_tier1 = max(0, tier1_after - tier1_before)
            runtime_stats.tier1_added_this_round = int(new_tier1)
            logger.info(
                "[SYSTEM_STATUS] Shadow Mode Active. Hydrating Seed_Whitelist...",
                extra={
                    "cycle_id": cycle_id,
                    "tier1_count_before": tier1_before,
                    "tier1_count_after": tier1_after,
                    "tier1_new_added": new_tier1,
                    "moralis_calls": runtime_stats.moralis.calls,
                    "moralis_estimated_cu": runtime_stats.moralis.estimated_cu,
                    "moralis_429": runtime_stats.moralis.errors_429,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discovery_cycle_failed", extra={"cycle_id": cycle_id, "error": str(exc)})
            next_interval_hours = max(1.0, cold_start_interval_hours)
        if run_once:
            break
        await asyncio.sleep(max(60.0, next_interval_hours * 3600.0))


def main() -> int:
    load_repo_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ap = argparse.ArgumentParser(description="Autonomous wallet discovery and DB hydration loop")
    ap.add_argument("--run-once", action="store_true", help="Run one discovery cycle and exit")
    ap.add_argument("--interval-hours", type=float, default=12.0, help="Loop interval in hours")
    ap.add_argument(
        "--provider",
        choices=("mock", "gamma_public", "dual_track"),
        default=os.getenv("DISCOVERY_PROVIDER", "dual_track"),
        help="Discovery provider route",
    )
    args = ap.parse_args()
    os.environ["DISCOVERY_PROVIDER"] = args.provider
    asyncio.run(discovery_main_loop(interval_hours=args.interval_hours, run_once=args.run_once))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
