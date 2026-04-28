#!/usr/bin/env python3
"""
Weekly lifecycle job for WATCHLIST_UNCERTAIN wallets.

Rules:
- trades >= 5 and win_rate >= 0.60 -> SMART_MONEY_QUANT
- trades >= 5 and win_rate < 0.40 -> NOISE (blacklist)
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger("evaluate_uncertain_wallets")


@dataclass(frozen=True)
class WalletPerf:
    address: str
    wins: int
    losses: int

    @property
    def trades(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float:
        if self.trades <= 0:
            return 0.0
        return self.wins / self.trades


def _fetch_watchlist_uncertain(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT address FROM watched_wallets WHERE label = 'WATCHLIST_UNCERTAIN' AND active = 1 ORDER BY created_ts_utc"
    ).fetchall()
    return [str(r[0]).lower() for r in rows]


def _perf_from_hunting_shadow(conn: sqlite3.Connection, address: str) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT outcome, COUNT(*)
        FROM hunting_shadow_hits
        WHERE lower(address) = ?
          AND outcome IN ('win','loss')
        GROUP BY outcome
        """,
        (address.lower(),),
    ).fetchall()
    wins = sum(int(c) for o, c in rows if o == "win")
    losses = sum(int(c) for o, c in rows if o == "loss")
    return wins, losses


def _perf_from_paper_trades(conn: sqlite3.Connection, address: str) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT outcome, COUNT(*)
        FROM paper_trades
        WHERE lower(coalesce(wallet_address,'')) = ?
          AND outcome IN ('win','loss')
        GROUP BY outcome
        """,
        (address.lower(),),
    ).fetchall()
    wins = sum(int(c) for o, c in rows if o == "win")
    losses = sum(int(c) for o, c in rows if o == "loss")
    return wins, losses


def evaluate_wallet(conn: sqlite3.Connection, address: str) -> WalletPerf:
    hw, hl = _perf_from_hunting_shadow(conn, address)
    pw, pl = _perf_from_paper_trades(conn, address)
    return WalletPerf(address=address, wins=hw + pw, losses=hl + pl)


def apply_transition(
    conn: sqlite3.Connection,
    *,
    address: str,
    to_label: str,
    deactivate: bool,
    dry_run: bool,
) -> None:
    if dry_run:
        logger.info("[DRY_RUN] transition address=%s -> %s active=%s", address, to_label, 0 if deactivate else 1)
        return
    conn.execute(
        """
        UPDATE watched_wallets
        SET label = ?, active = ?
        WHERE lower(address) = ?
        """,
        (to_label, 0 if deactivate else 1, address.lower()),
    )


def run(*, db_path: str, dry_run: bool, min_trades: int, promote_wr: float, evict_wr: float) -> int:
    p = Path(db_path)
    if not p.is_file():
        logger.error("db not found: %s", p)
        return 2
    conn = sqlite3.connect(p.as_posix())
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        wallets = _fetch_watchlist_uncertain(conn)
        logger.info("WATCHLIST_UNCERTAIN count=%s", len(wallets))
        promotes = 0
        evicts = 0
        for addr in wallets:
            perf = evaluate_wallet(conn, addr)
            logger.info(
                "wallet=%s trades=%s wins=%s losses=%s win_rate=%.4f",
                addr,
                perf.trades,
                perf.wins,
                perf.losses,
                perf.win_rate,
            )
            if perf.trades < min_trades:
                continue
            if perf.win_rate >= promote_wr:
                apply_transition(
                    conn,
                    address=addr,
                    to_label="SMART_MONEY_QUANT",
                    deactivate=False,
                    dry_run=dry_run,
                )
                promotes += 1
                continue
            if perf.win_rate < evict_wr:
                apply_transition(
                    conn,
                    address=addr,
                    to_label="NOISE",
                    deactivate=True,
                    dry_run=dry_run,
                )
                evicts += 1
        if not dry_run:
            conn.commit()
        logger.info("evaluate complete promotes=%s evicts=%s dry_run=%s", promotes, evicts, dry_run)
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly evaluator for WATCHLIST_UNCERTAIN wallets")
    ap.add_argument("--db-path", default="data/panopticon.db")
    ap.add_argument("--dry-run", action="store_true", help="Audit only; do not update labels")
    ap.add_argument("--min-trades", type=int, default=5)
    ap.add_argument("--promote-win-rate", type=float, default=0.60)
    ap.add_argument("--evict-win-rate", type=float, default=0.40)
    args = ap.parse_args()
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return run(
        db_path=args.db_path,
        dry_run=args.dry_run,
        min_trades=args.min_trades,
        promote_wr=args.promote_win_rate,
        evict_wr=args.evict_win_rate,
    )


if __name__ == "__main__":
    raise SystemExit(main())

