"""Fingerprint scrubber: Kelly / one-hit gates, three-tier labels, uncertain-bucket lifecycle."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from panopticon_py.hunting.four_d_classifier import EntityLabel, classify_high_frequency_wallet
from panopticon_py.hunting.trade_aggregate import ParentTrade

logger = logging.getLogger(__name__)

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
