"""Fingerprint scrubber: Kelly / one-hit gates, three-tier labels, uncertain-bucket lifecycle.

D171 Phase: Adds Shannon-entropy fingerprints (size, timing, market concentration)
for per-wallet behavioral scoring."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Literal

from panopticon_py.db import DBWriterQueue
from panopticon_py.hunting.four_d_classifier import EntityLabel, classify_high_frequency_wallet
from panopticon_py.hunting.trade_aggregate import ParentTrade
from panopticon_py.time_utils import utc_now_rfc3339_ms

PROCESS_VERSION = "v1.0.0-D171"

DEFAULT_SIZE_BUCKETS    = 10
DEFAULT_TIMING_BUCKETS  = 12
RECOMPUTE_INTERVAL_SEC  = 30 * 60
FETCH_CONCURRENCY      = 5
TRADES_PER_WALLET       = 500

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# D171: Shannon-entropy fingerprints for insider score w1 component
# ---------------------------------------------------------------------------


def _normalized_shannon(counts: Iterable[int]) -> float:
    counts = [c for c in counts if c > 0]
    if not counts:
        return 0.0
    total = float(sum(counts))
    if total <= 0 or len(counts) <= 1:
        return 0.0
    h = -sum((c / total) * math.log2(c / total) for c in counts)
    h_max = math.log2(len(counts))
    return h / h_max if h_max > 0 else 0.0


def size_entropy(sizes: list[float], n_buckets: int = DEFAULT_SIZE_BUCKETS) -> float:
    """Shannon entropy of trade-size distribution, normalised to [0, 1]."""
    if not sizes or n_buckets < 2:
        return 0.0
    log_sizes = [math.log10(max(s, 1e-9)) for s in sizes if s > 0]
    if len(log_sizes) < 2:
        return 0.0
    lo, hi = min(log_sizes), max(log_sizes)
    if hi - lo < 1e-9:
        return 0.0
    width = (hi - lo) / n_buckets
    buckets = [0] * n_buckets
    for x in log_sizes:
        idx = min(int((x - lo) / width), n_buckets - 1)
        buckets[idx] += 1
    return _normalized_shannon(buckets)


def timing_entropy(timestamps_sec: list[int], n_buckets: int = DEFAULT_TIMING_BUCKETS) -> float:
    """Entropy of inter-arrival times (log-spaced bins from 1 s to 1 week)."""
    if len(timestamps_sec) < 2:
        return 0.0
    sorted_ts = sorted(timestamps_sec)
    intervals = [t2 - t1 for t1, t2 in zip(sorted_ts, sorted_ts[1:]) if t2 > t1]
    if len(intervals) < 2:
        return 0.0
    lo_bound = math.log10(1)
    hi_bound = math.log10(60 * 60 * 24 * 7)  # 1s to 1 week
    width = (hi_bound - lo_bound) / n_buckets
    buckets = [0] * n_buckets
    for x in intervals:
        log_int = math.log10(max(x, 1))
        idx = max(0, min(int((log_int - lo_bound) / width), n_buckets - 1))
        buckets[idx] += 1
    return _normalized_shannon(buckets)


def market_concentration(categories: list[str]) -> dict:
    """Per-category share + max share (concentration ratio)."""
    if not categories:
        return {"max": 0.0}
    counts = Counter(categories)
    total = float(sum(counts.values()))
    shares = {cat: cnt / total for cat, cnt in counts.items()}
    shares["max"] = max(shares.values())
    return shares


def compute_fingerprint(wallet: str, trades: list[dict]) -> dict:
    """
    Build a fingerprint dict from a list of trade dicts.

    Expected trade dict shape (from DataAPIClient.fetch_user_trades):
      {size, timestamp_seconds, timestamp, market_id, category}
    """
    if not trades:
        return {
            "size_entropy":     0.0,
            "timing_entropy":  0.0,
            "concentration":  {"max": 0.0},
            "computed_at_utc": utc_now_rfc3339_ms(),
            "n_trades_sampled": 0,
        }
    sizes = []
    for t in trades:
        try:
            s = float(t.get("size", 0))
        except (TypeError, ValueError):
            continue
        if s > 0:
            sizes.append(s)
    timestamps = [
        int(t.get("timestamp_seconds") or t.get("timestamp") or 0)
        for t in trades
    ]
    timestamps = [t for t in timestamps if t > 0]
    cats = [str(t.get("category", "unknown")).lower() for t in trades]
    return {
        "size_entropy":    size_entropy(sizes),
        "timing_entropy": timing_entropy(timestamps),
        "concentration":  market_concentration(cats),
        "computed_at_utc": utc_now_rfc3339_ms(),
        "n_trades_sampled": len(trades),
    }


async def _recompute_one(client, wallet: str) -> None:
    try:
        trades = await client.fetch_user_trades(wallet, limit=TRADES_PER_WALLET)
    except Exception as exc:
        logger.warning("[FINGERPRINT][RECOMPUTE_ERR] wallet=%s fetch_err=%s", wallet[:10], exc)
        return
    fp = compute_fingerprint(wallet, trades)
    DBWriterQueue.put(
        "UPDATE wallet_watchlist SET score_components_json=? WHERE wallet_address=?",
        (json.dumps(fp), wallet),
        table_hint="wallet_watchlist",
    )


async def fingerprint_recompute_loop(close_event: asyncio.Event | None = None) -> None:
    """
    D171: Periodically recompute fingerprints for recently-active watchlist wallets.
    Args:
        close_event: if set, loop exits when event is triggered (D171 orchestrator wiring).
    """
    from panopticon_py.hunting.data_api_client import DataAPIClient

    db_path = os.environ.get("PANOPTICON_DB_PATH", "data/panopticon.db")
    client = DataAPIClient()
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    try:
        while True:
            # D171 Q1-A: respect orchestrator close_event
            if close_event is not None and close_event.is_set():
                logger.info("[FINGERPRINT] close_event set — exiting recompute loop")
                break

            try:
                with sqlite3.connect(db_path, timeout=10) as conn:
                    rows = conn.execute("""
                        SELECT wallet_address FROM wallet_watchlist
                        WHERE last_seen_ts_utc >= datetime('now', '-1 day')
                        LIMIT 200
                    """).fetchall()
            except Exception as exc:
                logger.warning("[FINGERPRINT] db read error: %s", exc)
                rows = []

            async def bounded(wallet: str) -> None:
                async with sem:
                    try:
                        await _recompute_one(client, wallet)
                    except Exception as exc:
                        logger.warning("[FINGERPRINT] wallet=%s err=%s", wallet[:10], exc)

            await asyncio.gather(*[bounded(r[0]) for r in rows], return_exceptions=True)
            logger.info("[FINGERPRINT] recompute pass done wallets=%d", len(rows))

            if close_event is not None:
                try:
                    await asyncio.wait_for(close_event.wait(), timeout=float(RECOMPUTE_INTERVAL_SEC))
                except asyncio.TimeoutError:
                    pass  # normal loop continuation
                # If we reach here without TimeoutError, event was set
                if close_event.is_set():
                    break
            else:
                await asyncio.sleep(RECOMPUTE_INTERVAL_SEC)
    finally:
        try:
            await client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Legacy fingerprint scrubber (D70-D160)
# ---------------------------------------------------------------------------

WalletLabel = Literal["SMART_MONEY_QUANT", "LONG_TERM_INSIDER", "WATCHLIST_UNCERTAIN", "NOISE"]
DiscoveryDropTag = Literal["MARKET_MAKER", "DEGEN_GAMBLER"]


@dataclass
class ScrubResult:
    address: str
    label: WalletLabel
    reasons: list[str] = field(default_factory=list)
    audit: dict[str, Any] = field(default_factory=dict)


@dataclass
class UncertainWalletState:
    address: str
    verified_profitable_trades: int = 0
    total_verified_trades: int = 0
    wins: int = 0
    last_eval_ts_utc: str | None = None
    last_trade_ts_utc: str | None = None
    parents_for_4d: list[ParentTrade] = field(default_factory=list)


@dataclass(frozen=True)
class WalletTradeSample:
    """單筆歷史樣本：供 discovery scrubber 做 IDI 與 Kelly 規則判定。"""

    side: int
    notional_usd: float
    balance_before_usd: float
    ts_ms: float
    market_id: str | None = None


@dataclass(frozen=True)
class DiscoveryWalletMetrics:
    wallet_address: str
    idi: float
    kelly_violation: bool
    drop_tag: DiscoveryDropTag | None
    reasons: list[str]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_kelly_violation(trades: list[dict[str, Any]], *, max_concentration: float | None = None) -> bool:
    """True if Kelly-style concentration proxy violated (single win dominates)."""
    thr = float(max_concentration if max_concentration is not None else os.getenv("SCRUB_KELLY_MAX_CONC", "0.85"))
    pnls = [float(t.get("realized_pnl_usd") or t.get("pnl") or 0.0) for t in trades]
    pos = [p for p in pnls if p > 0]
    if not pos:
        return False
    top = max(pos)
    s = sum(pos) or 1.0
    return (top / s) > thr


def check_one_hit_wonder(trades: list[dict[str, Any]], *, pnl_share: float | None = None) -> bool:
    thr = float(pnl_share if pnl_share is not None else os.getenv("SCRUB_ONE_HIT_SHARE", "0.7"))
    pnls = sorted([float(t.get("realized_pnl_usd") or t.get("pnl") or 0.0) for t in trades])
    if len(pnls) < 3:
        return False
    total = sum(pnls)
    if total <= 0:
        return False
    return abs(pnls[-1]) / abs(total) > thr


def _parents_from_trades(trades: list[dict[str, Any]], address: str) -> list[ParentTrade]:
    out: list[ParentTrade] = []
    for i, t in enumerate(sorted(trades, key=lambda x: float(x.get("ts_ms") or 0))):
        side = 1 if str(t.get("side") or "").upper() in {"BUY", "YES"} else -1
        vol = float(t.get("size") or t.get("notional_usd") or 0.0)
        ts = float(t.get("ts_ms") or i * 1000)
        out.append(
            ParentTrade(
                taker=address,
                side=side,
                volume=vol,
                first_ts_ms=ts,
                last_ts_ms=ts + 1.0,
                child_count=1,
                market_id=str(t.get("market_id") or ""),
            )
        )
    return out


def scrub_candidates(
    raw_candidates: list[dict[str, Any]],
    *,
    trades_by_address: dict[str, list[dict[str, Any]]] | None = None,
) -> list[ScrubResult]:
    """
    raw_candidates: items with at least ``address``.
    trades_by_address optional history for Kelly / one-hit / 4D.
    """
    trades_by_address = trades_by_address or {}
    results: list[ScrubResult] = []
    for row in raw_candidates:
        addr = str(row.get("address") or "").lower()
        if not addr.startswith("0x"):
            continue
        trades = trades_by_address.get(addr, [])
        reasons: list[str] = []
        if check_kelly_violation(trades):
            reasons.append("kelly_violation")
        if check_one_hit_wonder(trades):
            reasons.append("one_hit_wonder")
        parents = _parents_from_trades(trades, addr) if trades else []
        label_4d: EntityLabel | None = None
        if parents:
            label_4d, scores, r4 = classify_high_frequency_wallet(parents)
            reasons.extend(r4)
        else:
            reasons.append("no_trade_history")

        if "kelly_violation" in reasons or "one_hit_wonder" in reasons:
            lab: WalletLabel = "NOISE"
        elif label_4d == "INSIDER_ALGO_SLICING" or label_4d == "POTENTIAL_INSIDER":
            lab = "LONG_TERM_INSIDER"
        elif label_4d in ("MARKET_MAKER_NOISE", "COORDINATED_SMURF"):
            lab = "WATCHLIST_UNCERTAIN"
        elif label_4d in ("UNCERTAIN_NOISE", None):
            lab = "WATCHLIST_UNCERTAIN"
        else:
            lab = "SMART_MONEY_QUANT"

        audit = {"label_4d": label_4d, "reasons": list(reasons)}
        results.append(ScrubResult(address=addr, label=lab, reasons=reasons, audit=audit))
        logger.info("[SCRUB_RESULT] %s", {"address": addr, "label": lab, "audit": audit})
    return results


@dataclass
class BucketTransition:
    address: str
    from_label: WalletLabel
    to_label: WalletLabel
    reason: str


def evaluate_uncertain_bucket(
    uncertain_states: dict[str, UncertainWalletState],
    *,
    graduation_trades: int = 5,
    eviction_win_rate: float = 0.40,
    eviction_min_trades: int = 15,
    inactive_days: int = 90,
    now_utc: str | None = None,
) -> tuple[list[BucketTransition], dict[str, UncertainWalletState], list[str]]:
    """
    Weekly-style evaluation: promote, evict to NOISE, archive inactive.
    Returns (transitions, updated_uncertain_states, archived_addresses).
    """
    now_s = now_utc or _utc()
    transitions: list[BucketTransition] = []
    archived: list[str] = []
    to_delete: list[str] = []
    updated = dict(uncertain_states)

    for addr, st in list(updated.items()):
        st.last_eval_ts_utc = now_s
        parents = st.parents_for_4d
        label_4d: EntityLabel | None = None
        if parents:
            label_4d, _, _ = classify_high_frequency_wallet(parents)

        ok_4d = label_4d is not None and label_4d not in ("MARKET_MAKER_NOISE", "COORDINATED_SMURF")

        if st.verified_profitable_trades >= graduation_trades and ok_4d:
            transitions.append(
                BucketTransition(addr, "WATCHLIST_UNCERTAIN", "SMART_MONEY_QUANT", "graduation_profitable_4d")
            )
            to_delete.append(addr)
            logger.warning("[UNCERTAIN_GRADUATE] %s -> SMART_MONEY_QUANT", addr)
            continue

        wr = (st.wins / st.total_verified_trades) if st.total_verified_trades else 1.0
        if st.total_verified_trades >= eviction_min_trades and wr < eviction_win_rate:
            transitions.append(BucketTransition(addr, "WATCHLIST_UNCERTAIN", "NOISE", "low_win_rate"))
            to_delete.append(addr)
            logger.warning("[UNCERTAIN_EVICT_NOISE] %s win_rate=%s trades=%s", addr, wr, st.total_verified_trades)
            continue

        if st.last_trade_ts_utc:
            try:
                lt = datetime.fromisoformat(st.last_trade_ts_utc.replace("Z", "+00:00"))
                nw = datetime.fromisoformat(now_s.replace("Z", "+00:00"))
                if (nw - lt).days > inactive_days:
                    archived.append(addr)
                    to_delete.append(addr)
                    logger.info("[UNCERTAIN_ARCHIVE_INACTIVE] %s", addr)
            except ValueError:
                pass

    for a in to_delete:
        updated.pop(a, None)

    return transitions, updated, archived


def register_uncertain_from_scrub(results: list[ScrubResult]) -> dict[str, UncertainWalletState]:
    """Helper: build uncertain bucket dict from scrub output."""
    out: dict[str, UncertainWalletState] = {}
    for r in results:
        if r.label == "WATCHLIST_UNCERTAIN":
            out[r.address] = UncertainWalletState(address=r.address)
    return out


async def fetch_wallet_history(
    wallet: str,
    fetcher: Callable[[str], Awaitable[list[dict[str, Any]]]],
) -> list[WalletTradeSample]:
    """抽象歷史介面：由上層注入實際 provider（Gamma/Moralis）。"""
    raw = await fetcher(wallet)
    out: list[WalletTradeSample] = []
    for row in raw:
        side = 1 if str(row.get("side", "BUY")).upper() in {"BUY", "YES", "1"} else -1
        notional = float(row.get("notional_usd") or row.get("size_usd") or row.get("size") or 0.0)
        balance = float(row.get("balance_before_usd") or row.get("wallet_balance_usd") or 0.0)
        ts_ms = float(row.get("ts_ms") or row.get("timestamp_ms") or 0.0)
        out.append(
            WalletTradeSample(
                side=side,
                notional_usd=abs(notional),
                balance_before_usd=max(0.0, balance),
                ts_ms=ts_ms,
                market_id=str(row.get("market_id")) if row.get("market_id") else None,
            )
        )
    return out


def compute_idi(history: list[WalletTradeSample]) -> float:
    total = sum(abs(t.notional_usd) for t in history)
    if total <= 0:
        return 0.0
    net = sum((1 if t.side >= 0 else -1) * abs(t.notional_usd) for t in history)
    return abs(net) / total


def detect_kelly_violation(history: list[WalletTradeSample], *, ratio_threshold: float = 0.5) -> bool:
    for t in history:
        if t.balance_before_usd <= 0:
            continue
        if abs(t.notional_usd) / t.balance_before_usd >= ratio_threshold:
            return True
    return False


def scrub_wallet_for_discovery(
    wallet: str,
    history: list[WalletTradeSample],
    *,
    candidate_pnl: float = 0.0,
    candidate_source: str = "",
) -> DiscoveryWalletMetrics:
    """
    candidate_pnl / candidate_source: raw candidate signal passed through from
    the discovery track so we can still make a decision when history is empty.
    """
    reasons: list[str] = []
    idi = compute_idi(history)
    sample_size = len(history)

    # ── Case 1: No history at all ─────────────────────────────────────────────
    # Wallets with a credible source signal (Track A/B candidate with real PnL
    # or a named source) go to WATCHLIST_UNCERTAIN — they get tracked and
    # re-evaluated on the next cycle when/if history accumulates.
    if sample_size == 0:
        has_source_signal = candidate_pnl > 0 or bool(candidate_source)
        if has_source_signal:
            reasons.append("no_history_observed_with_source_signal")
            return DiscoveryWalletMetrics(
                wallet_address=wallet.lower(),
                idi=0.0,
                kelly_violation=False,
                drop_tag=None,  # → WATCHLIST_UNCERTAIN in calling code
                reasons=reasons,
            )
        # Nothing: no history AND no candidate signal — treat as potential MM
        reasons.append("no_history_no_signal_assumed_mm")
        return DiscoveryWalletMetrics(
            wallet_address=wallet.lower(),
            idi=0.0,
            kelly_violation=False,
            drop_tag="MARKET_MAKER",
            reasons=reasons,
        )

    # ── Case 2: Has history — normal IDI + Kelly filters ────────────────────────
    if idi < 0.3:
        reasons.append("idi_below_0.3_market_maker_like")
        return DiscoveryWalletMetrics(
            wallet_address=wallet.lower(),
            idi=idi,
            kelly_violation=False,
            drop_tag="MARKET_MAKER",
            reasons=reasons,
        )

    kelly_bad = detect_kelly_violation(history, ratio_threshold=0.5)
    if kelly_bad:
        reasons.append("single_trade_ge_50pct_balance")
        return DiscoveryWalletMetrics(
            wallet_address=wallet.lower(),
            idi=idi,
            kelly_violation=True,
            drop_tag="DEGEN_GAMBLER",
            reasons=reasons,
        )

    reasons.append("passed_idi_and_kelly_filters")
    return DiscoveryWalletMetrics(
        wallet_address=wallet.lower(),
        idi=idi,
        kelly_violation=False,
        drop_tag=None,
        reasons=reasons,
    )
