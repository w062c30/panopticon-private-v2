"""Funding trace with blacklist / tx_count pruning; CEX_ANONYMIZED → rely on 4D."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from panopticon_py.hunting.moralis_client import fetch_wallet_erc20_transfers_capped
from panopticon_py.rate_limit_governor import RateLimitGovernor


def load_cex_blacklist(path: str | None = None) -> set[str]:
    p = Path(path or os.getenv("HUNT_CEX_BLACKLIST_PATH", "")).expanduser()
    if not p.is_file():
        root = Path(__file__).resolve().parents[2]
        p = root / "config" / "cex_dex_routers_blacklist.json"
    if not p.is_file():
        return set()
    data = json.loads(p.read_text(encoding="utf-8"))
    addrs = data.get("addresses") if isinstance(data, dict) else []
    if not isinstance(addrs, list):
        return set()
    return {str(a).lower() for a in addrs if isinstance(a, str) and a.startswith("0x")}


def _counterparty(row: dict[str, Any], wallet: str) -> str:
    w = wallet.lower()
    fr = str(row.get("from_address") or row.get("from") or "").lower()
    to = str(row.get("to_address") or row.get("to") or "").lower()
    if fr == w:
        return to
    return fr


def trace_funding_roots(
    wallet: str,
    *,
    governor: RateLimitGovernor | None = None,
    tx_count_break: int | None = None,
) -> dict[str, Any]:
    """
    Shallow Moralis pull; break on blacklist or high-volume counterparties.

    Returns ``{"status": "...", "roots": [...], "cex_anonymized": bool}``.
    """
    brk = int(tx_count_break if tx_count_break is not None else os.getenv("HUNT_GRAPH_TX_COUNT_BREAK", "1000"))
    bl = load_cex_blacklist()
    rows = fetch_wallet_erc20_transfers_capped(wallet.lower()[:42], governor=governor)
    roots: list[str] = []
    cex_anon = False
    for row in rows:
        cp = _counterparty(row, wallet)
        if not cp.startswith("0x"):
            continue
        if cp in bl:
            cex_anon = True
            break
        roots.append(cp)
    max_unique = int(os.getenv("HUNT_MAX_UNIQUE_COUNTERPARTIES", "40"))
    if len(set(roots)) > max_unique:
        cex_anon = True
    status = "CEX_ANONYMIZED" if cex_anon else "OPEN"
    return {"status": status, "roots": list(dict.fromkeys(roots))[:20], "cex_anonymized": cex_anon}


def overlaps_seed(roots: list[str], seed_members: set[str]) -> bool:
    return bool(seed_members.intersection({r.lower()[:42] for r in roots}))


def derive_entity_id_from_roots(roots: list[str], wallet_address: str = "") -> str:
    """
    deterministic entity id:
    - sort + dedupe funding roots
    - hash into stable virtual entity key
    - When roots are empty, use wallet_address to prevent all wallets
      with empty funding roots being merged into a single entity.
    """
    normalized = sorted({r.lower()[:42] for r in roots if isinstance(r, str) and r.startswith("0x")})
    if normalized:
        basis = "|".join(normalized)
    elif wallet_address:
        # Each wallet with no funding roots gets its own entity ID
        # to prevent catastrophic Sybil merge
        basis = f"solo:{wallet_address.lower()[:42]}"
    else:
        basis = "no_roots"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f"ve_{digest}"


def temporal_sync_score(ts_ms_series_a: list[float], ts_ms_series_b: list[float], *, window_ms: float = 3000.0) -> float:
    """
    簡化時序同步指標：統計 A 在 window_ms 內能被 B 配對的比例。
    """
    if not ts_ms_series_a or not ts_ms_series_b:
        return 0.0
    a = sorted(ts_ms_series_a)
    b = sorted(ts_ms_series_b)
    j = 0
    hits = 0
    for t in a:
        while j < len(b) and b[j] < t - window_ms:
            j += 1
        if j < len(b) and abs(b[j] - t) <= window_ms:
            hits += 1
    return hits / max(1, len(a))


def sybil_group_wallets(
    candidates: list[dict[str, Any]],
    *,
    sync_threshold: float = 0.6,
) -> dict[str, list[str]]:
    """
    將錢包按 funding roots 與時序同步度分組成虛擬實體：
    - 同 roots key 直接合併
    - roots 不同但時序高度同步可附屬到同群（女巫腳本常見）
    """
    groups: dict[str, list[str]] = {}
    roots_by_wallet: dict[str, list[str]] = {}
    ts_by_wallet: dict[str, list[float]] = {}

    for row in candidates:
        wallet = str(row.get("wallet_address") or row.get("address") or "").lower()
        if not wallet.startswith("0x"):
            continue
        roots = [str(x).lower()[:42] for x in list(row.get("funding_roots") or []) if str(x).startswith("0x")]
        roots_by_wallet[wallet] = roots
        ts_by_wallet[wallet] = [float(x) for x in list(row.get("trade_ts_ms") or [])]
        eid = derive_entity_id_from_roots(roots, wallet)
        groups.setdefault(eid, [])
        if wallet not in groups[eid]:
            groups[eid].append(wallet)

    wallets = sorted(roots_by_wallet.keys())
    for i, wa in enumerate(wallets):
        for wb in wallets[i + 1 :]:
            if derive_entity_id_from_roots(roots_by_wallet[wa], wa) == derive_entity_id_from_roots(roots_by_wallet[wb], wb):
                continue
            sync = temporal_sync_score(ts_by_wallet.get(wa, []), ts_by_wallet.get(wb, []))
            if sync < sync_threshold:
                continue
            target = derive_entity_id_from_roots(roots_by_wallet[wa], wa)
            source = derive_entity_id_from_roots(roots_by_wallet[wb], wb)
            groups.setdefault(target, [])
            for addr in groups.get(source, []):
                if addr not in groups[target]:
                    groups[target].append(addr)
            groups.pop(source, None)

    return {eid: sorted(set(members)) for eid, members in groups.items() if members}
