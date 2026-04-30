from __future__ import annotations

import json
import logging
import math
import queue
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_utc_now = lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
from pathlib import Path
from typing import Any

from panopticon_py.time_utils import normalize_external_ts_to_utc, utc_now_rfc3339_ms
from panopticon_py.market_data.clob_series import fetch_settlement_price

logger = logging.getLogger(__name__)


def _utc() -> str:
    """ISO8601 UTC timestamp."""
    return utc_now_rfc3339_ms()


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS raw_events (
  event_id TEXT PRIMARY KEY,
  layer TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source TEXT NOT NULL,
  source_event_id TEXT,
  event_ts TEXT NOT NULL,
  ingest_ts_utc TEXT NOT NULL,
  version_tag TEXT NOT NULL,
  market_id TEXT,
  asset_id TEXT,
  payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy_decisions (
  decision_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  feature_snapshot_id TEXT NOT NULL,
  market_snapshot_id TEXT NOT NULL,
  prior_probability REAL NOT NULL CHECK(prior_probability >= 0 AND prior_probability <= 1),
  likelihood_ratio REAL NOT NULL CHECK(likelihood_ratio >= 0),
  posterior_probability REAL NOT NULL CHECK(posterior_probability >= 0 AND posterior_probability <= 1),
  ev_net REAL NOT NULL,
  kelly_fraction REAL NOT NULL CHECK(kelly_fraction >= 0),
  action TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL,
  FOREIGN KEY(event_id) REFERENCES raw_events(event_id)
);

CREATE TABLE IF NOT EXISTS execution_records (
  execution_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
  reason TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'PAPER' CHECK(mode IN ('LIVE', 'PAPER')),
  source TEXT NOT NULL DEFAULT 'radar' CHECK(source IN ('radar', 'ofi')),
  friction_snapshot_id TEXT,
  gate_reason TEXT,
  latency_bucket TEXT,
  toxicity_tag TEXT,
  tx_hash TEXT,
  settlement_status TEXT,
  confirmations INTEGER,
  mined_block_hash TEXT,
  clob_order_id TEXT,
  simulated_fill_price REAL,
  simulated_fill_size REAL,
  impact_pct REAL,
  latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
  created_ts_utc TEXT NOT NULL,
  p_adj REAL,
  qty REAL,
  ev_net REAL,
  avg_entry_price REAL,
  posterior REAL,
  market_tier TEXT NOT NULL DEFAULT 't3',
  market_id TEXT,
  asset_id TEXT
);
-- NOTE: execution_records has no FK to strategy_decisions.
-- decision_id is independently generated; FK removed per architect ruling (2026-04-26 Q1).

CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  cluster_id TEXT NOT NULL,
  side TEXT NOT NULL DEFAULT 'YES' CHECK(side IN ('YES','NO')),
  signed_notional_usd REAL NOT NULL DEFAULT 0,
  kelly_fraction REAL NOT NULL CHECK(kelly_fraction >= 0),
  opened_ts_utc TEXT NOT NULL,
  closed_ts_utc TEXT
);

CREATE TABLE IF NOT EXISTS pending_chain_events (
  tx_hash TEXT PRIMARY KEY,
  required_confirmations INTEGER NOT NULL,
  status TEXT NOT NULL,
  confirmations INTEGER NOT NULL DEFAULT 0,
  updated_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collateral_reservations (
  reservation_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  execution_id TEXT NOT NULL UNIQUE,
  amount_usdc REAL NOT NULL CHECK(amount_usdc >= 0),
  status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASED','FORFEITED')),
  reason TEXT,
  idempotency_key TEXT UNIQUE,
  created_ts_utc TEXT NOT NULL,
  FOREIGN KEY(decision_id) REFERENCES strategy_decisions(decision_id)
);

CREATE INDEX IF NOT EXISTS idx_collateral_reserved
  ON collateral_reservations(status) WHERE status = 'RESERVED';
CREATE INDEX IF NOT EXISTS idx_collateral_decision
  ON collateral_reservations(decision_id);

CREATE TABLE IF NOT EXISTS correlation_edges (
  market_a TEXT NOT NULL,
  market_b TEXT NOT NULL,
  rho REAL NOT NULL,
  window_sec INTEGER NOT NULL,
  updated_ts_utc TEXT NOT NULL,
  PRIMARY KEY (market_a, market_b, window_sec)
);

CREATE INDEX IF NOT EXISTS idx_corr_updated ON correlation_edges(updated_ts_utc);

CREATE TABLE IF NOT EXISTS watched_wallets (
  address TEXT PRIMARY KEY,
  label TEXT,
  source TEXT,
  created_ts_utc TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_watched_active ON watched_wallets(active) WHERE active = 1;

CREATE TABLE IF NOT EXISTS wallet_observations (
  obs_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  market_id TEXT,
  obs_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  ingest_ts_utc TEXT NOT NULL,
  transaction_hash TEXT,
  order_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_wallet_obs_address ON wallet_observations(address);
CREATE INDEX IF NOT EXISTS idx_wallet_obs_ingest ON wallet_observations(ingest_ts_utc);

CREATE TABLE IF NOT EXISTS insider_score_snapshots (
  score_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  market_id TEXT,
  score REAL NOT NULL CHECK(score >= 0 AND score <= 1),
  reasons_json TEXT NOT NULL,
  ingest_ts_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_insider_address ON insider_score_snapshots(address);
CREATE INDEX IF NOT EXISTS idx_insider_ingest ON insider_score_snapshots(ingest_ts_utc);

CREATE TABLE IF NOT EXISTS hunting_shadow_hits (
  hit_id TEXT PRIMARY KEY,
  address TEXT NOT NULL,
  market_id TEXT,
  entity_score REAL,
  entropy_z REAL,
  sim_pnl_proxy REAL,
  outcome TEXT,
  payload_json TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hunting_shadow_addr ON hunting_shadow_hits(address);
CREATE INDEX IF NOT EXISTS idx_hunting_shadow_ts ON hunting_shadow_hits(created_ts_utc);

CREATE TABLE IF NOT EXISTS kyle_lambda_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    TEXT    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    delta_price REAL    NOT NULL,
    trade_size  REAL    NOT NULL,
    lambda_obs  REAL    NOT NULL,
    market_id   TEXT,
    source      TEXT    DEFAULT 'standalone',
    created_at  TEXT    NOT NULL,
    window_ts   INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_kls_asset_ts ON kyle_lambda_samples(asset_id, ts_utc);

CREATE TABLE IF NOT EXISTS paper_trades (
  paper_trade_id TEXT PRIMARY KEY,
  decision_id TEXT NOT NULL,
  wallet_address TEXT,
  market_id TEXT NOT NULL,
  cluster_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK(side IN ('YES','NO')),
  sizing_notional REAL NOT NULL CHECK(sizing_notional >= 0),
  kelly_fraction REAL NOT NULL CHECK(kelly_fraction >= 0),
  cluster_delta_before REAL,
  cluster_delta_after REAL,
  reason TEXT NOT NULL,
  outcome TEXT,
  created_ts_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_trades_decision ON paper_trades(decision_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_cluster ON paper_trades(cluster_id);
CREATE INDEX IF NOT EXISTS idx_paper_trades_created ON paper_trades(created_ts_utc);

CREATE TABLE IF NOT EXISTS realized_pnl_settlement (
  trade_id TEXT PRIMARY KEY,
  paper_trade_id TEXT,
  decision_id TEXT,
  market_id TEXT NOT NULL,
  event_name TEXT,
  direction TEXT CHECK(direction IN ('YES','NO')),
  confidence REAL,
  open_reason TEXT,
  close_reason TEXT,
  close_condition TEXT,
  entry_price REAL,
  exit_price REAL,
  position_size_usd REAL NOT NULL DEFAULT 0,
  estimated_ev_usd REAL NOT NULL DEFAULT 0,
  realized_pnl_usd REAL NOT NULL DEFAULT 0,
  opened_ts_utc TEXT NOT NULL,
  closed_ts_utc TEXT NOT NULL,
  source_event TEXT
);

CREATE INDEX IF NOT EXISTS idx_settlement_closed_ts ON realized_pnl_settlement(closed_ts_utc);
CREATE INDEX IF NOT EXISTS idx_settlement_market ON realized_pnl_settlement(market_id);
CREATE INDEX IF NOT EXISTS idx_settlement_decision ON realized_pnl_settlement(decision_id);

CREATE TABLE IF NOT EXISTS virtual_entity_events (
  event_id TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  members_json TEXT NOT NULL,
  classification TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_virtual_entity_ts ON virtual_entity_events(created_ts_utc);

CREATE TABLE IF NOT EXISTS discovered_entities (
  entity_id TEXT PRIMARY KEY,
  trust_score REAL NOT NULL CHECK(trust_score >= 0 AND trust_score <= 100),
  primary_tag TEXT NOT NULL,
  sample_size INTEGER NOT NULL DEFAULT 0 CHECK(sample_size >= 0),
  last_updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovered_entities_updated ON discovered_entities(last_updated_at);
CREATE INDEX IF NOT EXISTS idx_discovered_entities_tag ON discovered_entities(primary_tag);

CREATE TABLE IF NOT EXISTS tracked_wallets (
  wallet_address TEXT PRIMARY KEY,
  entity_id TEXT NOT NULL,
  all_time_pnl REAL NOT NULL DEFAULT 0,
  win_rate REAL NOT NULL DEFAULT 0 CHECK(win_rate >= 0 AND win_rate <= 1),
  discovery_source TEXT NOT NULL,
  source_quality TEXT NOT NULL DEFAULT 'unknown',
  history_sample_size INTEGER NOT NULL DEFAULT 0,
  last_seen_ts_utc TEXT,
  last_updated_at TEXT NOT NULL,
  FOREIGN KEY(entity_id) REFERENCES discovered_entities(entity_id)
);
CREATE INDEX IF NOT EXISTS idx_tracked_wallets_entity ON tracked_wallets(entity_id);
CREATE INDEX IF NOT EXISTS idx_tracked_wallets_source ON tracked_wallets(discovery_source);
CREATE INDEX IF NOT EXISTS idx_tracked_wallets_updated ON tracked_wallets(last_updated_at);

CREATE TABLE IF NOT EXISTS wallet_funding_roots (
  wallet_address TEXT NOT NULL,
  roots_json     TEXT NOT NULL,
  updated_ts_utc TEXT NOT NULL,
  PRIMARY KEY (wallet_address)
);

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  reason TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS polymarket_link_map (
  market_id TEXT PRIMARY KEY,
  token_id TEXT,
  event_slug TEXT,
  market_slug TEXT,
  canonical_event_url TEXT,
  canonical_embed_url TEXT,
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_poly_link_token
  ON polymarket_link_map(token_id) WHERE token_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS polymarket_link_unresolved (
  unresolved_id TEXT PRIMARY KEY,
  market_id TEXT,
  token_id TEXT,
  event_name TEXT,
  reason TEXT NOT NULL,
  source TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL,
  resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1))
);

CREATE INDEX IF NOT EXISTS idx_poly_unresolved_open
  ON polymarket_link_unresolved(resolved, created_ts_utc);

CREATE TABLE IF NOT EXISTS pending_entropy_signals (
    signal_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT,
    entropy_z REAL NOT NULL,
    sim_pnl_proxy REAL,
    trigger_address TEXT NOT NULL,
    trigger_ts_utc TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT,
    created_ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(signal_id)
);

CREATE INDEX IF NOT EXISTS idx_pending_entropy_signals_unconsumed
    ON pending_entropy_signals(market_id, consumed_at)
    WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS wallet_market_positions (
    wallet_address TEXT NOT NULL,
    market_id TEXT NOT NULL,
    current_position_notional REAL NOT NULL DEFAULT 0.0,
    avg_entry_price REAL NOT NULL DEFAULT 0.0,
    last_updated_ts_utc TEXT NOT NULL,
    PRIMARY KEY (wallet_address, market_id)
);

CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_wallet
    ON wallet_market_positions(wallet_address);

CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_market
    ON wallet_market_positions(market_id);

CREATE TABLE IF NOT EXISTS insider_pattern_flags (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address       TEXT NOT NULL,
    asset_id             TEXT NOT NULL,
    detected_ts_utc      TEXT NOT NULL,
    case_type            TEXT,          -- 'SOLO_OP' | 'CLUSTER' | 'DECOY_CLUSTER'
    account_age_days     REAL,
    prior_at_bet         REAL,          -- market YES probability when bet placed
    stake_usd            REAL,
    correlated_mkts      INTEGER DEFAULT 0,
    cluster_wallet_count INTEGER DEFAULT 1,
    same_ts_wallets      INTEGER DEFAULT 0,
    has_decoy_bets       INTEGER DEFAULT 0,
    pattern_score        REAL DEFAULT 0.0,
    flag_reason          TEXT,
    human_reviewed       INTEGER DEFAULT 0,
    created_ts_utc       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ipf_wallet ON insider_pattern_flags(wallet_address);
CREATE INDEX IF NOT EXISTS idx_ipf_score  ON insider_pattern_flags(pattern_score DESC);
CREATE INDEX IF NOT EXISTS idx_ipf_ts     ON insider_pattern_flags(detected_ts_utc);
"""


class ShadowDB:
    # ── Batch write buffers (reduce SSD writes) ────────────────────────────────
    # Each buffer holds rows as dicts; flushed via executemany + single commit.
    _wallet_obs_buffer: list[dict[str, Any]] = []   # append_wallet_observation
    _kyle_buffer: list[dict[str, Any]] = []          # append_kyle_lambda_sample
    _flush_lock: threading.Lock = threading.Lock()    # guard concurrent flush
    _snapshot_lock: threading.Lock = threading.Lock()   # D89: guard insider_score snapshot writes
    _BATCH_SIZE = 50                                  # flush every N rows

    def __init__(self, db_path: str = "data/panopticon.db") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path.as_posix(), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row   # D113: enables row["column"] access — fully backward compatible
        self.conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode: readers don't block writers, writers don't block readers.
        # Critical for running Radar + OFI + Graph + Discovery all on same DB.
        self.conn.execute("PRAGMA journal_mode=WAL;")
        # Wait up to 30s for locks instead of immediately failing.
        # Allows Hyperliquid OFI engine and Polymarket radar to coexist with
        # start_shadow_hydration.py's atomic_execution_and_reserve BEGIN IMMEDIATE.
        self.conn.execute("PRAGMA busy_timeout=30000;")
        # [Q6 Ruling] NORMAL synchronous = good balance of safety and performance.
        # WAL mode already handles most durability concerns.
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        # Advisory lock table for preventing dual-orchestrator crashes.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _process_locks (
                lock_key TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                ttl_sec INTEGER NOT NULL DEFAULT 3600
            )
            """
        )

    def bootstrap(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self._ensure_execution_columns()
        self._ensure_positions_columns()
        self._ensure_collateral_and_correlation_tables()
        self._ensure_observation_tables()
        self._ensure_hunting_tables()
        self._ensure_signal_engine_tables()
        self._ensure_discovery_tables()
        self._ensure_pol_watchlist_table()
        self._ensure_funding_roots_table()
        self._ensure_paper_trade_tables()
        self._ensure_settlement_tables()
        self._ensure_polymarket_link_tables()
        self._ensure_insider_pattern_flags_table()
        self._ensure_pipeline_health_table()
        self._ensure_identity_coverage_table()
        self._ensure_series_tables()
        self.conn.commit()

    # D80: Expose sqlite3.Connection.execute for callers that expect a raw cursor.
    # Fixes: AttributeError "'ShadowDB' object has no attribute 'execute'" in
    # run_insider_monitor at run_hft_orchestrator.py:L491.
    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, parameters)

    def _ensure_execution_columns(self) -> None:
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(execution_records)").fetchall()
        }
        required = [
            ("friction_snapshot_id", "TEXT"),
            ("gate_reason", "TEXT"),
            ("latency_bucket", "TEXT"),
            ("toxicity_tag", "TEXT"),
            ("tx_hash", "TEXT"),
            ("settlement_status", "TEXT"),
            ("confirmations", "INTEGER"),
            ("mined_block_hash", "TEXT"),
            ("clob_order_id", "TEXT"),
            ("mode", "TEXT NOT NULL DEFAULT 'PAPER'"),
            ("source", "TEXT NOT NULL DEFAULT 'radar'"),
            # Phase 2-C-1: p_adj, qty, ev_net, avg_entry_price
            ("p_adj", "REAL"),
            ("qty", "REAL"),
            ("ev_net", "REAL"),
            ("avg_entry_price", "REAL"),
            # Phase 2-C-2: Bayesian posterior (pre-gate, raw consensus output)
            ("posterior", "REAL"),
            # Market tier tag: t1/t2/t3/t5 (for p_prior override per Invariant 1.4)
            ("market_tier", "TEXT NOT NULL DEFAULT 't3'"),
            # D46: market_id and asset_id for routing and auditing
            ("market_id", "TEXT"),
            ("asset_id", "TEXT"),
        ]
        for col, col_type in required:
            if col not in existing:
                try:
                    self.conn.execute(f"ALTER TABLE execution_records ADD COLUMN {col} {col_type}")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower():
                        logger.warning(
                            "[DB] execution_records column '%s' skipped — DB locked by another writer; "
                            "column will be added on next restart when lock is free",
                            col,
                        )
                    else:
                        raise

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_def: str) -> None:
        """
        Idempotent ALTER TABLE ADD COLUMN — no-op if column already exists.
        Use this instead of raw ALTER TABLE in all _ensure_* migration functions.
        """
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")

    def _ensure_collateral_and_correlation_tables(self) -> None:
        """CREATE TABLE IF NOT EXISTS for upgrades from older DB files."""
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS collateral_reservations (
              reservation_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              execution_id TEXT NOT NULL UNIQUE,
              amount_usdc REAL NOT NULL CHECK(amount_usdc >= 0),
              status TEXT NOT NULL CHECK(status IN ('RESERVED','RELEASED','FORFEITED')),
              reason TEXT,
              idempotency_key TEXT UNIQUE,
              created_ts_utc TEXT NOT NULL,
              FOREIGN KEY(decision_id) REFERENCES strategy_decisions(decision_id)
            );
            CREATE INDEX IF NOT EXISTS idx_collateral_reserved
              ON collateral_reservations(status) WHERE status = 'RESERVED';
            CREATE INDEX IF NOT EXISTS idx_collateral_decision
              ON collateral_reservations(decision_id);

            CREATE TABLE IF NOT EXISTS correlation_edges (
              market_a TEXT NOT NULL,
              market_b TEXT NOT NULL,
              rho REAL NOT NULL,
              window_sec INTEGER NOT NULL,
              updated_ts_utc TEXT NOT NULL,
              PRIMARY KEY (market_a, market_b, window_sec)
            );
            CREATE INDEX IF NOT EXISTS idx_corr_updated ON correlation_edges(updated_ts_utc);
            """
        )

    def _ensure_positions_columns(self) -> None:
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(positions)").fetchall()
        }
        required = [
            ("side", "TEXT NOT NULL DEFAULT 'YES'"),
            ("signed_notional_usd", "REAL NOT NULL DEFAULT 0"),
        ]
        for col, col_type in required:
            if col not in existing:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {col_type}")

    def _ensure_paper_trade_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
              paper_trade_id TEXT PRIMARY KEY,
              decision_id TEXT NOT NULL,
              wallet_address TEXT,
              market_id TEXT NOT NULL,
              cluster_id TEXT NOT NULL,
              side TEXT NOT NULL CHECK(side IN ('YES','NO')),
              sizing_notional REAL NOT NULL CHECK(sizing_notional >= 0),
              kelly_fraction REAL NOT NULL CHECK(kelly_fraction >= 0),
              cluster_delta_before REAL,
              cluster_delta_after REAL,
              reason TEXT NOT NULL,
              outcome TEXT,
              created_ts_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_decision ON paper_trades(decision_id);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_cluster ON paper_trades(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_paper_trades_created ON paper_trades(created_ts_utc);
            """
        )
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()
        }
        if "wallet_address" not in existing:
            self.conn.execute("ALTER TABLE paper_trades ADD COLUMN wallet_address TEXT")

    def _ensure_settlement_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS realized_pnl_settlement (
              trade_id TEXT PRIMARY KEY,
              paper_trade_id TEXT,
              decision_id TEXT,
              market_id TEXT NOT NULL,
              event_name TEXT,
              direction TEXT CHECK(direction IN ('YES','NO')),
              confidence REAL,
              open_reason TEXT,
              close_reason TEXT,
              close_condition TEXT,
              entry_price REAL,
              exit_price REAL,
              position_size_usd REAL NOT NULL DEFAULT 0,
              estimated_ev_usd REAL NOT NULL DEFAULT 0,
              realized_pnl_usd REAL NOT NULL DEFAULT 0,
              opened_ts_utc TEXT NOT NULL,
              closed_ts_utc TEXT NOT NULL,
              source_event TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_settlement_closed_ts ON realized_pnl_settlement(closed_ts_utc);
            CREATE INDEX IF NOT EXISTS idx_settlement_market ON realized_pnl_settlement(market_id);
            CREATE INDEX IF NOT EXISTS idx_settlement_decision ON realized_pnl_settlement(decision_id);
            """
        )

    def _ensure_polymarket_link_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS polymarket_link_map (
              market_id TEXT PRIMARY KEY,
              token_id TEXT,
              event_slug TEXT,
              market_slug TEXT,
              canonical_event_url TEXT,
              canonical_embed_url TEXT,
              source TEXT NOT NULL,
              fetched_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_poly_link_token
              ON polymarket_link_map(token_id) WHERE token_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS polymarket_link_unresolved (
              unresolved_id TEXT PRIMARY KEY,
              market_id TEXT,
              token_id TEXT,
              event_name TEXT,
              reason TEXT NOT NULL,
              source TEXT NOT NULL,
              created_ts_utc TEXT NOT NULL,
              resolved INTEGER NOT NULL DEFAULT 0 CHECK(resolved IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS idx_poly_unresolved_open
              ON polymarket_link_unresolved(resolved, created_ts_utc);
            """
        )

    def _ensure_observation_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS watched_wallets (
              address TEXT PRIMARY KEY,
              label TEXT,
              source TEXT,
              created_ts_utc TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS idx_watched_active ON watched_wallets(active) WHERE active = 1;

            CREATE TABLE IF NOT EXISTS wallet_observations (
              obs_id TEXT PRIMARY KEY,
              address TEXT NOT NULL,
              market_id TEXT,
              obs_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              ingest_ts_utc TEXT NOT NULL,
              transaction_hash TEXT,
              order_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_obs_address ON wallet_observations(address);
            CREATE INDEX IF NOT EXISTS idx_wallet_obs_ingest ON wallet_observations(ingest_ts_utc);

            CREATE TABLE IF NOT EXISTS insider_score_snapshots (
              score_id TEXT PRIMARY KEY,
              address TEXT NOT NULL,
              market_id TEXT,
              score REAL NOT NULL CHECK(score >= 0 AND score <= 1),
              reasons_json TEXT NOT NULL,
              ingest_ts_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_insider_address ON insider_score_snapshots(address);
            CREATE INDEX IF NOT EXISTS idx_insider_ingest ON insider_score_snapshots(ingest_ts_utc);
            """
        )

    def _ensure_hunting_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS hunting_shadow_hits (
              hit_id TEXT PRIMARY KEY,
              address TEXT NOT NULL,
              market_id TEXT,
              entity_score REAL,
              entropy_z REAL,
              sim_pnl_proxy REAL,
              outcome TEXT,
              payload_json TEXT NOT NULL,
              created_ts_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_hunting_shadow_addr ON hunting_shadow_hits(address);
            CREATE INDEX IF NOT EXISTS idx_hunting_shadow_ts ON hunting_shadow_hits(created_ts_utc);

            CREATE TABLE IF NOT EXISTS virtual_entity_events (
              event_id TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL,
              members_json TEXT NOT NULL,
              classification TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_ts_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_virtual_entity_ts ON virtual_entity_events(created_ts_utc);
            """
        )

    def _ensure_signal_engine_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_entropy_signals (
                signal_id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                token_id TEXT,
                entropy_z REAL NOT NULL,
                sim_pnl_proxy REAL,
                trigger_address TEXT NOT NULL,
                trigger_ts_utc TEXT NOT NULL,
                consumed_at TEXT,
                consumed_by TEXT,
                created_ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(signal_id)
            );
            CREATE INDEX IF NOT EXISTS idx_pending_entropy_signals_unconsumed
                ON pending_entropy_signals(market_id, consumed_at)
                WHERE consumed_at IS NULL;

            CREATE TABLE IF NOT EXISTS wallet_market_positions (
                wallet_address TEXT NOT NULL,
                market_id TEXT NOT NULL,
                current_position_notional REAL NOT NULL DEFAULT 0.0,
                avg_entry_price REAL NOT NULL DEFAULT 0.0,
                last_updated_ts_utc TEXT NOT NULL,
                PRIMARY KEY (wallet_address, market_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_wallet
                ON wallet_market_positions(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_wallet_market_positions_market
                ON wallet_market_positions(market_id);
            """
        )

    def _ensure_insider_pattern_flags_table(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS insider_pattern_flags (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address       TEXT NOT NULL,
                asset_id             TEXT NOT NULL,
                detected_ts_utc      TEXT NOT NULL,
                case_type            TEXT,
                account_age_days     REAL,
                prior_at_bet         REAL,
                stake_usd            REAL,
                correlated_mkts      INTEGER DEFAULT 0,
                cluster_wallet_count INTEGER DEFAULT 1,
                same_ts_wallets      INTEGER DEFAULT 0,
                has_decoy_bets       INTEGER DEFAULT 0,
                pattern_score        REAL DEFAULT 0.0,
                flag_reason          TEXT,
                human_reviewed       INTEGER DEFAULT 0,
                created_ts_utc       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_ipf_wallet ON insider_pattern_flags(wallet_address);
            CREATE INDEX IF NOT EXISTS idx_ipf_score  ON insider_pattern_flags(pattern_score DESC);
            CREATE INDEX IF NOT EXISTS idx_ipf_ts     ON insider_pattern_flags(detected_ts_utc);
            """
        )

    def _ensure_pipeline_health_table(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pipeline_health (
                id                         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc                    TEXT NOT NULL,
                window_minutes             INTEGER NOT NULL,

                -- L1 Ingestion
                l1_trade_ticks_received   INTEGER,
                l1_trade_ticks_by_tier    TEXT,
                l1_entropy_fires          INTEGER,
                l1_entropy_fires_by_tier  TEXT,
                l1_kyle_samples_written   INTEGER,

                -- L2/L3 Signal Processing
                l2_signal_events_queued    INTEGER,
                l3_bayesian_updates      INTEGER,
                l3_gate_pass             INTEGER,
                l3_gate_reject           INTEGER,

                -- L4 Execution
                l4_paper_trades          INTEGER,
                l4_live_trades           INTEGER,

                -- L5 Data Quality
                l5_wallet_obs_written    INTEGER,
                l5_insider_score_updates INTEGER,

                -- Derived
                l1_tick_rate_per_min     REAL,
                pipeline_pass_rate        REAL,
                kyle_accumulation_rate   REAL,
                data_staleness_flag      INTEGER,
                notes                    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ph_ts ON pipeline_health(ts_utc);
            """
        )

        # ── Schema migrations for existing DBs ────────────────────────────────
        # D113: unified via _add_column_if_missing
        self._add_column_if_missing(
            self.conn, "kyle_lambda_samples", "window_ts", "INTEGER DEFAULT 0"
        )

        # D70/D113: Add missing columns to polymarket_link_map for BTC 5m resolution
        for col, col_def in [
            ("slug", "TEXT"),
            ("condition_id", "TEXT"),
            ("market_tier", "TEXT"),
            ("created_at", "TEXT"),
        ]:
            self._add_column_if_missing(self.conn, "polymarket_link_map", col, col_def)

        # ── RVF Live Metrics Snapshots ──────────────────────────────────────
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rvf_metrics_snapshots (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc           TEXT NOT NULL,

                -- WS / L1
                ws_connected      INTEGER,
                ws_t1            INTEGER,
                ws_t2            INTEGER,
                ws_t3            INTEGER,
                ws_t5            INTEGER,
                trade_ticks_60s  INTEGER,
                book_events_60s  INTEGER,
                t1_window_start  INTEGER,
                t1_window_end    INTEGER,
                secs_remaining    REAL,
                t1_rollover_cnt  INTEGER,
                elapsed_since_ws REAL,

                -- Kyle
                kyle_samples     INTEGER,
                kyle_assets      INTEGER,
                kyle_p75         REAL,
                kyle_last_elapsed REAL,
                kyle_last_status TEXT,

                -- Window
                active_ew        INTEGER,
                last_cleanup_cnt  INTEGER,
                last_cleanup_ts  REAL,

                -- Queue
                queue_depth      INTEGER,
                processed_60s    INTEGER,
                mean_p_t1        REAL,
                mean_p_t2        REAL,
                mean_z_t1        REAL,
                mean_z_t2        REAL,

                -- Gate
                gate_eval_60s    INTEGER,
                gate_pass_60s    INTEGER,
                gate_abort_60s   INTEGER,
                paper_trades     INTEGER,
                paper_winrate    REAL,
                avg_ev           REAL,

                -- Series
                deadline_ladders INTEGER,
                rolling_windows  INTEGER,
                total_series     INTEGER,
                monotone_viol   INTEGER,
                last_viol_slug  TEXT,
                last_viol_gap   REAL,
                catalysts_today  INTEGER,
                oracle_high      INTEGER,

                -- Readiness
                readiness_kyle_pct  REAL,
                readiness_trades_pct REAL,
                readiness_winrate_pct REAL,
                readiness_all_ready  INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_rvf_ts ON rvf_metrics_snapshots(ts_utc);
            """
        )

    def _ensure_identity_coverage_table(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS identity_coverage_log (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id               TEXT NOT NULL,
                asset_id                TEXT,
                window_ts               INTEGER DEFAULT 0,
                market_tier             TEXT NOT NULL,
                event_slug              TEXT,
                window_start_utc        TEXT NOT NULL,
                window_end_utc          TEXT NOT NULL,
                poll_interval_sec       REAL NOT NULL DEFAULT 4.0,
                ws_trade_ticks          INTEGER NOT NULL DEFAULT 0,
                api_trades_received     INTEGER NOT NULL DEFAULT 0,
                api_trades_with_wallet  INTEGER NOT NULL DEFAULT 0,
                estimated_loss_rate     REAL,
                wallet_coverage_rate    REAL,
                api_page_saturated      INTEGER DEFAULT 0,
                event_total_ws_ticks    INTEGER DEFAULT 0,
                event_total_api_trades  INTEGER DEFAULT 0,
                event_cumulative_loss   REAL,
                created_at              TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_icl_market_ts
                ON identity_coverage_log(market_id, window_start_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_icl_tier_ts
                ON identity_coverage_log(market_tier, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_icl_window_ts
                ON identity_coverage_log(window_ts, asset_id)
                WHERE window_ts > 0;
            """
        )

    def write_identity_coverage(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO identity_coverage_log (
                market_id, asset_id, window_ts, market_tier, event_slug,
                window_start_utc, window_end_utc, poll_interval_sec,
                ws_trade_ticks, api_trades_received, api_trades_with_wallet,
                estimated_loss_rate, wallet_coverage_rate, api_page_saturated,
                event_total_ws_ticks, event_total_api_trades, event_cumulative_loss,
                created_at
            ) VALUES (
                :market_id, :asset_id, :window_ts, :market_tier, :event_slug,
                :window_start_utc, :window_end_utc, :poll_interval_sec,
                :ws_trade_ticks, :api_trades_received, :api_trades_with_wallet,
                :estimated_loss_rate, :wallet_coverage_rate, :api_page_saturated,
                :event_total_ws_ticks, :event_total_api_trades, :event_cumulative_loss,
                :created_at
            )
            """,
            row,
        )
        self.conn.commit()

    def fetch_coverage_by_event(self, market_id: str, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT market_id, asset_id, window_ts, market_tier, event_slug,
                   window_start_utc, window_end_utc, ws_trade_ticks,
                   api_trades_received, api_trades_with_wallet,
                   estimated_loss_rate, wallet_coverage_rate,
                   api_page_saturated, event_cumulative_loss, created_at
            FROM identity_coverage_log
            WHERE market_id = ?
            ORDER BY window_start_utc DESC LIMIT ?
            """,
            (market_id, limit),
        ).fetchall()
        cols = [
            "market_id", "asset_id", "window_ts", "market_tier", "event_slug",
            "window_start_utc", "window_end_utc", "ws_trade_ticks",
            "api_trades_received", "api_trades_with_wallet",
            "estimated_loss_rate", "wallet_coverage_rate",
            "api_page_saturated", "event_cumulative_loss", "created_at",
        ]
        return [dict(zip(cols, r)) for r in rows]

    def fetch_coverage_summary(self, market_tier: str | None = None) -> dict:
        where_clause = "WHERE market_tier = ? AND" if market_tier else "WHERE"
        args: tuple = (market_tier,) if market_tier else ()
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(DISTINCT market_id)                           AS distinct_markets,
                COUNT(*)                                            AS total_polls,
                AVG(estimated_loss_rate)                            AS avg_loss_rate,
                MAX(estimated_loss_rate)                            AS max_loss_rate,
                AVG(wallet_coverage_rate)                           AS avg_wallet_coverage,
                SUM(CASE WHEN api_page_saturated=1 THEN 1 ELSE 0 END) AS saturated_polls,
                SUM(ws_trade_ticks)                                 AS total_ws_ticks,
                SUM(api_trades_received)                            AS total_api_trades
            FROM identity_coverage_log
            {where_clause} created_at > datetime('now', '-24 hours')
            """,
            args,
        ).fetchone()
        if not row:
            return {}
        cols = [
            "distinct_markets", "total_polls", "avg_loss_rate", "max_loss_rate",
            "avg_wallet_coverage", "saturated_polls", "total_ws_ticks", "total_api_trades",
        ]
        return dict(zip(cols, row))

    def _ensure_series_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_series (
                series_id        TEXT PRIMARY KEY,
                series_type      TEXT NOT NULL,
                -- DEADLINE_LADDER | ROLLING_WINDOW | CORRELATED_TOPIC
                underlying_topic TEXT,
                oracle_risk      TEXT DEFAULT 'UNKNOWN',
                -- LOW | MEDIUM | HIGH | UNKNOWN
                created_ts_utc   TEXT NOT NULL,
                last_updated_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS series_members (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                series_id        TEXT REFERENCES event_series(series_id),
                token_id         TEXT NOT NULL,
                slug             TEXT NOT NULL UNIQUE,
                settlement_date  TEXT,
                market_tier      TEXT DEFAULT 't2',
                last_prob        REAL DEFAULT 0.5,
                last_updated_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_series_members_token
                ON series_members(token_id);
            CREATE INDEX IF NOT EXISTS idx_series_members_series
                ON series_members(series_id);

            CREATE TABLE IF NOT EXISTS series_violations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          TEXT NOT NULL,
                series_id      TEXT,
                violation_type  TEXT NOT NULL,
                -- MONOTONE_VIOLATION | PRE_CATALYST_SIGNAL | SMART_EXIT | LEAD_LAG
                earlier_slug    TEXT,
                later_slug      TEXT,
                gap_pct         REAL,
                wallet_address  TEXT,
                action_taken    TEXT DEFAULT 'LOGGED'
            );

            CREATE INDEX IF NOT EXISTS idx_series_violations_ts
                ON series_violations(ts_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_series_violations_series
                ON series_violations(series_id);

            CREATE TABLE IF NOT EXISTS wallet_series_positions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                wallet_address   TEXT NOT NULL,
                series_id        TEXT NOT NULL,
                slug             TEXT NOT NULL,
                side             TEXT NOT NULL,
                -- YES | NO
                avg_entry_prob   REAL,
                total_size       REAL,
                trade_count      INTEGER DEFAULT 1,
                first_seen_utc   TEXT NOT NULL,
                last_seen_utc    TEXT NOT NULL,
                position_status  TEXT DEFAULT 'OPEN',
                -- OPEN | CLOSED | PARTIALLY_CLOSED
                exit_prob        REAL,
                exit_ts_utc      TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_wsp_wallet_series
                ON wallet_series_positions(wallet_address, series_id);
            CREATE INDEX IF NOT EXISTS idx_wsp_status
                ON wallet_series_positions(position_status, last_seen_utc DESC);

            CREATE TABLE IF NOT EXISTS catalyst_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc         TEXT NOT NULL,
                market_id      TEXT NOT NULL,
                slug           TEXT NOT NULL,
                series_id      TEXT,
                z_score        REAL NOT NULL,
                prob_before    REAL,
                prob_after     REAL,
                prob_delta     REAL,
                lookback_done  INTEGER DEFAULT 0,
                wallets_found  INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_catalyst_events_ts
                ON catalyst_events(ts_utc DESC);
            CREATE INDEX IF NOT EXISTS idx_catalyst_events_market
                ON catalyst_events(market_id, ts_utc DESC);
            """
        )

    def _ensure_discovery_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovered_entities (
              entity_id TEXT PRIMARY KEY,
              trust_score REAL NOT NULL CHECK(trust_score >= 0 AND trust_score <= 100),
              primary_tag TEXT NOT NULL,
              sample_size INTEGER NOT NULL DEFAULT 0 CHECK(sample_size >= 0),
              last_updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovered_entities_updated ON discovered_entities(last_updated_at);
            CREATE INDEX IF NOT EXISTS idx_discovered_entities_tag ON discovered_entities(primary_tag);

            CREATE TABLE IF NOT EXISTS tracked_wallets (
              wallet_address TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL,
              all_time_pnl REAL NOT NULL DEFAULT 0,
              win_rate REAL NOT NULL DEFAULT 0 CHECK(win_rate >= 0 AND win_rate <= 1),
              discovery_source TEXT NOT NULL,
              source_quality TEXT NOT NULL DEFAULT 'unknown',
              history_sample_size INTEGER NOT NULL DEFAULT 0,
              last_seen_ts_utc TEXT,
              last_updated_at TEXT NOT NULL,
              FOREIGN KEY(entity_id) REFERENCES discovered_entities(entity_id)
            );
            CREATE INDEX IF NOT EXISTS idx_tracked_wallets_entity ON tracked_wallets(entity_id);
            CREATE INDEX IF NOT EXISTS idx_tracked_wallets_source ON tracked_wallets(discovery_source);
            CREATE INDEX IF NOT EXISTS idx_tracked_wallets_updated ON tracked_wallets(last_updated_at);
            """
        )
        # D113: unified via _add_column_if_missing (replaces manual PRAGMA + if-not-in-existing pattern)
        for col, col_def in [
            ("source_quality", "TEXT NOT NULL DEFAULT 'unknown'"),
            ("history_sample_size", "INTEGER NOT NULL DEFAULT 0"),
            ("last_seen_ts_utc", "TEXT"),
            ("discovery_source", "TEXT NOT NULL DEFAULT 'unknown'"),
        ]:
            self._add_column_if_missing(self.conn, "tracked_wallets", col, col_def)

        # discovered_entities column migrations — already using _add_column_if_missing (keep as-is)
        self._add_column_if_missing(self.conn, "discovered_entities", "discovery_source", "TEXT NOT NULL DEFAULT 'unknown'")
        # D82: insider_score for CONSENSUS_SYNC metrics
        self._add_column_if_missing(self.conn, "discovered_entities", "insider_score", "REAL DEFAULT 0.0")

        # D96: order_reconstructions table
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS order_reconstructions (
                order_id            TEXT PRIMARY KEY,
                taker_wallet        TEXT NOT NULL,
                market_id           TEXT NOT NULL,
                side                TEXT NOT NULL,
                total_size          REAL NOT NULL,
                fill_count          INTEGER NOT NULL DEFAULT 1,
                avg_price           REAL NOT NULL,
                first_fill_ts       INTEGER NOT NULL,
                last_fill_ts        INTEGER NOT NULL,
                is_complete         INTEGER NOT NULL DEFAULT 0,
                order_type_inferred TEXT,
                created_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_or_open
                ON order_reconstructions(taker_wallet, market_id, side, is_complete, last_fill_ts);
            CREATE INDEX IF NOT EXISTS idx_or_ts
                ON order_reconstructions(last_fill_ts);
        """)

        # D96: wallet_observations extended columns
        self._add_column_if_missing(self.conn, "wallet_observations", "transaction_hash", "TEXT")
        self._add_column_if_missing(self.conn, "wallet_observations", "order_id", "TEXT")

        # D101: T2-POL political market watchlist — moved to _ensure_pol_watchlist_table()

    def _ensure_pol_watchlist_table(self) -> None:
        """D102/D111/D112: Unified migration via _add_column_if_missing."""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pol_market_watchlist (
                market_id          TEXT PRIMARY KEY,
                token_id           TEXT,
                token_id_no        TEXT,              -- D111: NO-side token (clob[1])
                event_slug         TEXT,
                political_category TEXT NOT NULL
                    CHECK(political_category IN (
                        'ELECTION','LEGISLATION','APPOINTMENT',
                        'GEOPOLITICAL','POLICY','OTHER'
                    )),
                entity_keywords    TEXT NOT NULL,   -- JSON array e.g. '["trump","tariff"]'
                subscribed_at      TEXT NOT NULL,
                last_signal_ts     TEXT,
                is_active          INTEGER NOT NULL DEFAULT 1
                    CHECK(is_active IN (0,1))
            );
            CREATE INDEX IF NOT EXISTS idx_pol_active
                ON pol_market_watchlist(is_active, subscribed_at DESC);
        """)
        # D112: Use unified helper instead of bare try/except
        self._add_column_if_missing(self.conn, "pol_market_watchlist", "token_id_no", "TEXT")

    def upsert_pol_market(self, row: dict) -> None:
        """D101: Upsert a political market into pol_market_watchlist."""
        self.conn.execute(
            """
            INSERT INTO pol_market_watchlist
                (market_id, token_id, token_id_no, event_slug, political_category,
                 entity_keywords, subscribed_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(market_id) DO UPDATE SET
                token_id=excluded.token_id,
                token_id_no=excluded.token_id_no,
                event_slug=excluded.event_slug,
                political_category=excluded.political_category,
                entity_keywords=excluded.entity_keywords,
                is_active=1
            """,
            (
                row["market_id"],
                row.get("token_id"),
                row.get("token_id_no"),  # D111: NO-side token
                row.get("event_slug"),
                row["political_category"],
                json.dumps(row.get("entity_keywords", [])),
                row["subscribed_at"],
            ),
        )
        self.conn.commit()

    def fetch_active_pol_markets(self) -> list[dict]:
        """D112: Use column-name mapping instead of positional index to prevent silent breakage."""
        rows = self.conn.execute("""
            SELECT market_id, token_id, token_id_no, event_slug, political_category,
                   entity_keywords, subscribed_at, last_signal_ts
            FROM pol_market_watchlist
            WHERE is_active = 1
            ORDER BY subscribed_at DESC
        """).fetchall()
        cols = [
            "market_id", "token_id", "token_id_no", "event_slug",
            "political_category", "entity_keywords", "subscribed_at", "last_signal_ts",
        ]
        result = []
        for r in rows:
            row_dict = dict(zip(cols, r))
            row_dict["entity_keywords"] = json.loads(row_dict["entity_keywords"] or "[]")
            result.append(row_dict)
        return result

    def update_pol_last_signal_ts(self, market_id: str, ts: str) -> None:
        """D102: Record last signal timestamp for a political market."""
        self.conn.execute(
            "UPDATE pol_market_watchlist SET last_signal_ts = ? WHERE market_id = ?",
            (ts, market_id),
        )
        self.conn.commit()

    def deactivate_closed_pol_markets(self, active_market_ids: set[str]) -> int:
        """
        D102: Set is_active=0 for any pol_market_watchlist entry
        whose market_id is NOT in active_market_ids.
        Returns deactivated count.
        """
        if not active_market_ids:
            return 0
        placeholders = ",".join("?" * len(active_market_ids))
        cur = self.conn.execute(
            f"UPDATE pol_market_watchlist SET is_active=0 "
            f"WHERE is_active=1 AND market_id NOT IN ({placeholders})",
            tuple(active_market_ids),
        )
        self.conn.commit()
        return cur.rowcount

    def _ensure_funding_roots_table(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wallet_funding_roots (
              wallet_address TEXT NOT NULL,
              roots_json     TEXT NOT NULL,
              updated_ts_utc TEXT NOT NULL,
              PRIMARY KEY (wallet_address)
            );
            """
        )

    def append_hunting_shadow_hit(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO hunting_shadow_hits (
              hit_id, address, market_id, entity_score, entropy_z, sim_pnl_proxy, outcome, payload_json, created_ts_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["hit_id"],
                row["address"].lower(),
                row.get("market_id"),
                row.get("entity_score"),
                row.get("entropy_z"),
                row.get("sim_pnl_proxy"),
                row.get("outcome"),
                row["payload_json"] if isinstance(row["payload_json"], str) else json.dumps(row["payload_json"], ensure_ascii=False),
                row["created_ts_utc"],
            ),
        )
        self.conn.commit()

    # ── D21: Event Series ────────────────────────────────────────────────────────

    def upsert_event_series(self, series: dict) -> None:
        """Insert or update an event_series row."""
        self.conn.execute(
            """
            INSERT INTO event_series (series_id, series_type, underlying_topic, oracle_risk, created_ts_utc, last_updated_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(series_id) DO UPDATE SET
                series_type=excluded.series_type,
                underlying_topic=excluded.underlying_topic,
                oracle_risk=excluded.oracle_risk,
                last_updated_utc=excluded.last_updated_utc
            """,
            (
                series["series_id"],
                series["series_type"],
                series.get("underlying_topic", ""),
                series.get("oracle_risk", "UNKNOWN"),
                series.get("created_ts_utc", _utc()),
                _utc(),
            ),
        )
        self.conn.commit()

    def upsert_series_member(self, series_id: str, member: dict) -> None:
        """Insert or update a series_members row."""
        self.conn.execute(
            """
            INSERT INTO series_members (series_id, token_id, slug, settlement_date, market_tier, last_prob, last_updated_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                series_id=excluded.series_id,
                token_id=excluded.token_id,
                settlement_date=excluded.settlement_date,
                market_tier=excluded.market_tier,
                last_prob=excluded.last_prob,
                last_updated_utc=excluded.last_updated_utc
            """,
            (
                series_id,
                member["token_id"],
                member["slug"],
                member.get("settlement_date", ""),
                member.get("market_tier", "t2"),
                member.get("current_prob", 0.5),
                _utc(),
            ),
        )
        self.conn.commit()

    def write_series_violation(
        self,
        series_id: str | None = None,
        violation_type: str = "",
        earlier_slug: str | None = None,
        later_slug: str | None = None,
        gap_pct: float | None = None,
        wallet_address: str | None = None,
        action_taken: str = "LOGGED",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO series_violations
                (ts_utc, series_id, violation_type, earlier_slug, later_slug, gap_pct, wallet_address, action_taken)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc(),
                series_id,
                violation_type,
                earlier_slug,
                later_slug,
                gap_pct,
                wallet_address,
                action_taken,
            ),
        )
        self.conn.commit()

    def write_catalyst_event(self, row: dict) -> None:
        self.conn.execute(
            """
            INSERT INTO catalyst_events
                (ts_utc, market_id, slug, series_id, z_score, prob_before, prob_after, prob_delta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("ts_utc", _utc()),
                row["market_id"],
                row.get("slug", ""),
                row.get("series_id", ""),
                row["z_score"],
                row.get("prob_before", 0.5),
                row.get("prob_after", 0.5),
                row.get("prob_delta", 0.0),
            ),
        )
        self.conn.commit()

    def mark_lookback_done(self, market_id: str, catalyst_ts_iso: str, wallets_found: int) -> None:
        self.conn.execute(
            """
            UPDATE catalyst_events
            SET lookback_done=1, wallets_found=?
            WHERE market_id=? AND ts_utc=?
            """,
            (wallets_found, market_id, catalyst_ts_iso),
        )
        self.conn.commit()

    def upsert_wallet_series_position(
        self,
        wallet_address: str,
        series_id: str,
        slug: str,
        side: str,
        trade_prob: float,
        trade_size: float,
        ts_utc: str,
    ) -> None:
        """
        Upsert a wallet_series_positions entry.
        If position exists: update avg_entry_prob, total_size, trade_count, last_seen_utc.
        If new: insert with OPEN status.
        """
        existing = self.conn.execute(
            "SELECT id, avg_entry_prob, total_size, trade_count FROM wallet_series_positions "
            "WHERE wallet_address=? AND slug=? AND position_status='OPEN'",
            (wallet_address, slug),
        ).fetchone()

        if existing:
            old_avg = existing["avg_entry_prob"] or 0.0
            old_total = existing["total_size"] or 0.0
            old_count = existing["trade_count"] or 1
            new_count = old_count + 1
            new_avg = (old_avg * old_count + trade_prob) / new_count
            new_total = old_total + trade_size
            self.conn.execute(
                "UPDATE wallet_series_positions SET "
                "avg_entry_prob=?, total_size=?, trade_count=?, last_seen_utc=? "
                "WHERE wallet_address=? AND slug=? AND position_status='OPEN'",
                (new_avg, new_total, new_count, ts_utc, wallet_address, slug),
            )
        else:
            self.conn.execute(
                "INSERT INTO wallet_series_positions "
                "(wallet_address, series_id, slug, side, avg_entry_prob, total_size, trade_count, first_seen_utc, last_seen_utc, position_status) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'OPEN')",
                (wallet_address, series_id, slug, side, trade_prob, trade_size, ts_utc, ts_utc),
            )
        self.conn.commit()

    def update_wallet_series_position_exit(
        self, wallet_address: str, slug: str, exit_prob: float, sell_ratio: float
    ) -> None:
        """
        Mark position as PARTIALLY_CLOSED or CLOSED based on sell_ratio.
        If remaining_size / original_size < 10%, mark CLOSED.
        """
        pos = self.conn.execute(
            "SELECT total_size FROM wallet_series_positions WHERE wallet_address=? AND slug=? AND position_status='OPEN'",
            (wallet_address, slug),
        ).fetchone()
        if not pos:
            return
        original = pos["total_size"] or 1e-8
        remaining = original * (1 - sell_ratio)
        new_status = "CLOSED" if remaining / original < 0.10 else "PARTIALLY_CLOSED"
        self.conn.execute(
            "UPDATE wallet_series_positions SET "
            "position_status=?, exit_prob=?, exit_ts_utc=?, total_size=? "
            "WHERE wallet_address=? AND slug=? AND position_status='OPEN'",
            (new_status, exit_prob, _utc(), remaining, wallet_address, slug),
        )
        self.conn.commit()

    def get_wallet_series_position(self, wallet_address: str, slug: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM wallet_series_positions WHERE wallet_address=? AND slug=? AND position_status='OPEN'",
            (wallet_address, slug),
        ).fetchone()
        return dict(row) if row else None

    def get_series_id_for_market(self, token_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT series_id FROM series_members WHERE token_id=?", (token_id,),
        ).fetchone()
        return row["series_id"] if row else None

    def query_pre_catalyst_wallets(
        self,
        market_id: str,
        start_ts: str,
        end_ts: str,
        side: str = "YES",
        min_trade_count: int = 2,
        max_entry_prob: float = 0.25,
    ) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT
                wallet_address,
                COUNT(*)       AS trade_count,
                AVG(price)     AS avg_entry_prob,
                SUM(size)      AS total_size,
                MIN(ts_utc)    AS first_trade_ts,
                MAX(ts_utc)    AS last_trade_ts
            FROM wallet_observations
            WHERE market_id    = :market_id
              AND side        = :side
              AND price       <= :max_entry_prob
              AND ts_utc      >= :start_ts
              AND ts_utc      <  :end_ts
            GROUP BY wallet_address
            HAVING COUNT(*) >= :min_trade_count
            ORDER BY trade_count DESC, total_size DESC
            LIMIT 50
            """,
            {
                "market_id": market_id,
                "side": side,
                "max_entry_prob": max_entry_prob,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "min_trade_count": min_trade_count,
            },
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_insider_score(self, wallet_address: str) -> float | None:
        row = self.conn.execute(
            "SELECT pattern_score FROM insider_pattern_flags "
            "WHERE wallet_address=? ORDER BY detected_ts_utc DESC LIMIT 1",
            (wallet_address,),
        ).fetchone()
        return row["pattern_score"] if row else None

    def update_insider_score(self, wallet_address: str, new_score: float, reason: str) -> None:
        # Upgrade pattern_score for this wallet (observational only)
        self.conn.execute(
            "UPDATE insider_pattern_flags SET pattern_score=? "
            "WHERE wallet_address=? ORDER BY detected_ts_utc DESC LIMIT 1",
            (new_score, wallet_address),
        )
        self.conn.commit()

    def append_virtual_entity_event(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO virtual_entity_events (event_id, entity_id, members_json, classification, payload_json, created_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                row["event_id"],
                row["entity_id"],
                row["members_json"] if isinstance(row["members_json"], str) else json.dumps(row["members_json"], ensure_ascii=False),
                row["classification"],
                row["payload_json"] if isinstance(row["payload_json"], str) else json.dumps(row["payload_json"], ensure_ascii=False),
                row["created_ts_utc"],
            ),
        )
        self.conn.commit()

    def upsert_discovered_entity(self, row: dict[str, Any]) -> None:
        # D101: insider_score write path — conditional INSERT/UPDATE to avoid
        # SQLite ambiguous column error when using COALESCE in ON CONFLICT.
        insider_score = row.get("insider_score")  # float | None

        if insider_score is not None:
            self.conn.execute(
                """
                INSERT INTO discovered_entities
                    (entity_id, trust_score, primary_tag, sample_size, last_updated_at, insider_score)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    trust_score=excluded.trust_score,
                    primary_tag=excluded.primary_tag,
                    sample_size=excluded.sample_size,
                    last_updated_at=excluded.last_updated_at,
                    insider_score=excluded.insider_score
                """,
                (
                    row["entity_id"],
                    float(row["trust_score"]),
                    str(row.get("primary_tag") or "UNKNOWN"),
                    int(row.get("sample_size") or 0),
                    str(row["last_updated_at"]),
                    float(insider_score),
                ),
            )
        else:
            # No insider_score provided — preserve existing value (backward compatible)
            self.conn.execute(
                """
                INSERT INTO discovered_entities (entity_id, trust_score, primary_tag, sample_size, last_updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    trust_score=excluded.trust_score,
                    primary_tag=excluded.primary_tag,
                    sample_size=excluded.sample_size,
                    last_updated_at=excluded.last_updated_at
                """,
                (
                    row["entity_id"],
                    float(row["trust_score"]),
                    str(row.get("primary_tag") or "UNKNOWN"),
                    int(row.get("sample_size") or 0),
                    str(row["last_updated_at"]),
                ),
            )
        self.conn.commit()

    def fetch_t5_coverage_summary(self) -> dict:
        """
        D101: T5 體育市場 Coverage 面板資料。
        返回 24 小時內 T5 市場的信號、執行、勝率摘要。

        Note: Only counts execution_records with market_tier='t5'.
        Historical records with NULL market_tier are excluded (D46+ tagged records only).
        """
        row = self.conn.execute("""
            SELECT
                COUNT(*)                                                AS total_signals,
                SUM(CASE WHEN accepted=1 THEN 1 ELSE 0 END)            AS accepted,
                SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END)            AS rejected,
                AVG(CASE WHEN accepted=1 THEN ev_net ELSE NULL END)     AS avg_ev_accepted,
                AVG(posterior)                                          AS avg_posterior,
                COUNT(DISTINCT market_id)                               AS distinct_markets
            FROM execution_records
            WHERE market_tier = 't5'
              AND created_ts_utc > datetime('now', '-24 hours')
        """).fetchone()

        if not row or not row[0]:
            return {
                "tier": "t5",
                "period": "24h",
                "total_signals": 0,
                "accepted": 0,
                "rejected": 0,
                "avg_ev_accepted": None,
                "avg_posterior": None,
                "distinct_markets": 0,
                "pass_rate": None,
            }

        total = int(row[0])
        accepted = int(row[1] or 0)
        return {
            "tier": "t5",
            "period": "24h",
            "total_signals": total,
            "accepted": accepted,
            "rejected": int(row[2] or 0),
            "avg_ev_accepted": float(row[3]) if row[3] is not None else None,
            "avg_posterior": float(row[4]) if row[4] is not None else None,
            "distinct_markets": int(row[5] or 0),
            "pass_rate": round(accepted / total, 3) if total > 0 else None,
        }

    def upsert_tracked_wallet(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO tracked_wallets (
              wallet_address, entity_id, all_time_pnl, win_rate, discovery_source, source_quality, history_sample_size, last_seen_ts_utc, last_updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
              entity_id=excluded.entity_id,
              all_time_pnl=excluded.all_time_pnl,
              win_rate=excluded.win_rate,
              discovery_source=excluded.discovery_source,
              source_quality=excluded.source_quality,
              history_sample_size=excluded.history_sample_size,
              last_seen_ts_utc=excluded.last_seen_ts_utc,
              last_updated_at=excluded.last_updated_at
            """,
            (
                str(row["wallet_address"]).lower(),
                str(row["entity_id"]),
                float(row.get("all_time_pnl") or 0.0),
                float(row.get("win_rate") or 0.0),
                str(row.get("discovery_source") or "unknown"),
                str(row.get("source_quality") or "unknown"),
                int(row.get("history_sample_size") or 0),
                row.get("last_seen_ts_utc"),
                str(row["last_updated_at"]),
            ),
        )
        self.conn.commit()

    def upsert_wallet_funding_roots(self, wallet_address: str, roots: list[str], updated_ts_utc: str) -> None:
        """
        Persist funding roots for a wallet so FundingRootCache (HFT path) can
        retrieve them without hitting Moralis.

        Funding roots are the counterparty addresses that funded this wallet via
        ERC20 transfer (computed by ``trace_funding_roots()`` in the discovery loop).
        """
        self.conn.execute(
            """
            INSERT INTO wallet_funding_roots (wallet_address, roots_json, updated_ts_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(wallet_address) DO UPDATE SET
              roots_json=excluded.roots_json,
              updated_ts_utc=excluded.updated_ts_utc
            """,
            (wallet_address.lower()[:42], json.dumps(roots[:20]), updated_ts_utc),
        )
        self.conn.commit()

    def fetch_wallet_funding_roots(self, wallet_address: str) -> list[str]:
        """
        Return the persisted funding-root list for a wallet, or [] if none found.
        Called by ``_load_roots_from_db()`` in the HFT graph_linker as the
        cache-first DB path per [Invariant 2.4].
        """
        row = self.conn.execute(
            "SELECT roots_json FROM wallet_funding_roots WHERE wallet_address = ?",
            (wallet_address.lower()[:42],),
        ).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return []
        return []

    def append_discovery_audit(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO audit_log (audit_id, actor, action, before_json, after_json, reason, created_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(row["audit_id"]),
                str(row.get("actor") or "wallet_discovery"),
                str(row["action"]),
                row.get("before_json")
                if isinstance(row.get("before_json"), str) or row.get("before_json") is None
                else json.dumps(row.get("before_json"), ensure_ascii=False),
                row.get("after_json")
                if isinstance(row.get("after_json"), str) or row.get("after_json") is None
                else json.dumps(row.get("after_json"), ensure_ascii=False),
                str(row.get("reason") or "discovery_hydration"),
                str(row["created_ts_utc"]),
            ),
        )
        self.conn.commit()

    def hunting_shadow_win_rate(self, *, min_rows: int = 5) -> float | None:
        rows = self.conn.execute(
            "SELECT outcome FROM hunting_shadow_hits WHERE outcome IN ('win','loss')"
        ).fetchall()
        if len(rows) < min_rows:
            return None
        wins = sum(1 for r in rows if r[0] == "win")
        return wins / max(1, len(rows))

    def append_raw_event(self, envelope: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO raw_events (
              event_id, layer, event_type, source, source_event_id, event_ts, ingest_ts_utc,
              version_tag, market_id, asset_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope["event_id"],
                envelope["layer"],
                envelope["event_type"],
                envelope["source"],
                envelope.get("source_event_id"),
                envelope["event_ts"],
                envelope["ingest_ts_utc"],
                envelope["version_tag"],
                envelope.get("market_id"),
                envelope.get("asset_id"),
                json.dumps(envelope["payload"], ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        # Flush pending buffers before closing so no rows are lost
        self.flush_wallet_obs_buffer()
        self.flush_kyle_buffer()
        self.conn.close()

    def append_strategy_decision(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO strategy_decisions (
              decision_id, event_id, feature_snapshot_id, market_snapshot_id, prior_probability,
              likelihood_ratio, posterior_probability, ev_net, kelly_fraction, action, created_ts_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["decision_id"],
                row["event_id"],
                row["feature_snapshot_id"],
                row["market_snapshot_id"],
                row["prior_probability"],
                row["likelihood_ratio"],
                row["posterior_probability"],
                row["ev_net"],
                row["kelly_fraction"],
                row["action"],
                row["created_ts_utc"],
            ),
        )
        self.conn.commit()

    def append_execution_record(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO execution_records (
              execution_id, decision_id, accepted, reason, mode, source,
              friction_snapshot_id, gate_reason,
              latency_bucket, toxicity_tag, tx_hash, settlement_status, confirmations,
              mined_block_hash, clob_order_id,
              simulated_fill_price, simulated_fill_size,
              impact_pct, latency_ms, created_ts_utc,
              p_adj, qty, ev_net, avg_entry_price, posterior,
              market_tier, market_id, asset_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["execution_id"],
                row["decision_id"],
                row["accepted"],
                row["reason"],
                row.get("mode", "PAPER"),
                row.get("source", "radar"),
                row.get("friction_snapshot_id"),
                row.get("gate_reason"),
                row.get("latency_bucket"),
                row.get("toxicity_tag"),
                row.get("tx_hash"),
                row.get("settlement_status"),
                row.get("confirmations"),
                row.get("mined_block_hash"),
                row.get("clob_order_id"),
                row.get("simulated_fill_price"),
                row.get("simulated_fill_size"),
                row.get("impact_pct"),
                row["latency_ms"],
                row["created_ts_utc"],
                row.get("p_adj"),
                row.get("qty"),
                row.get("ev_net"),
                row.get("avg_entry_price"),
                row.get("posterior"),
                row.get("market_tier", "t3"),
                row.get("market_id"),   # D46: was added to signal_engine but not wired here
                row.get("asset_id"),
            ),
        )
        self.conn.commit()

    def upsert_pending_chain(self, tx_hash: str, required_confirmations: int, status: str, confirmations: int, ts: str) -> None:
        self.conn.execute(
            """
            INSERT INTO pending_chain_events (tx_hash, required_confirmations, status, confirmations, updated_ts_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tx_hash) DO UPDATE SET
              status=excluded.status,
              confirmations=excluded.confirmations,
              updated_ts_utc=excluded.updated_ts_utc
            """,
            (tx_hash, required_confirmations, status, confirmations, ts),
        )
        self.conn.commit()

    def update_execution_settlement(
        self,
        tx_hash: str,
        confirmations: int,
        status: str,
        *,
        mined_block_hash: str | None = None,
    ) -> None:
        if mined_block_hash is not None:
            self.conn.execute(
                """
                UPDATE execution_records
                SET confirmations = ?, settlement_status = ?, mined_block_hash = COALESCE(?, mined_block_hash)
                WHERE tx_hash = ?
                """,
                (confirmations, status, mined_block_hash, tx_hash),
            )
        else:
            self.conn.execute(
                """
                UPDATE execution_records
                SET confirmations = ?, settlement_status = ?
                WHERE tx_hash = ?
                """,
                (confirmations, status, tx_hash),
            )
        self.conn.commit()

    def sum_active_reserved_usdc(self) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) FROM collateral_reservations WHERE status = 'RESERVED'"
        ).fetchone()
        return float(row[0] if row and row[0] is not None else 0.0)

    def atomic_execution_and_reserve(
        self,
        *,
        execution: dict[str, Any],
        reservation_id: str,
        amount_usdc: float,
        idempotency_key: str | None,
        created_ts_utc: str,
    ) -> None:
        """Single-writer transaction: persist execution then lock collateral (BEGIN IMMEDIATE)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """
                INSERT INTO execution_records (
                  execution_id, decision_id, accepted, reason, mode, source,
                  friction_snapshot_id, gate_reason,
                  latency_bucket, toxicity_tag, tx_hash, settlement_status, confirmations,
                  mined_block_hash, clob_order_id,
                  simulated_fill_price, simulated_fill_size,
                  impact_pct, latency_ms, created_ts_utc,
                  p_adj, qty, ev_net, avg_entry_price, posterior,
                  market_tier, market_id, asset_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution["execution_id"],
                    execution["decision_id"],
                    execution["accepted"],
                    execution["reason"],
                    execution.get("mode", "PAPER"),
                    execution.get("source", "radar"),
                    execution.get("friction_snapshot_id"),
                    execution.get("gate_reason"),
                    execution.get("latency_bucket"),
                    execution.get("toxicity_tag"),
                    execution.get("tx_hash"),
                    execution.get("settlement_status", "not_submitted"),
                    execution.get("confirmations"),
                    execution.get("mined_block_hash"),
                    execution.get("clob_order_id"),
                    execution.get("simulated_fill_price"),
                    execution.get("simulated_fill_size"),
                    execution.get("impact_pct"),
                    execution["latency_ms"],
                    execution["created_ts_utc"],
                    execution.get("p_adj"),
                    execution.get("qty"),
                    execution.get("ev_net"),
                    execution.get("avg_entry_price"),
                    execution.get("posterior"),
                    execution.get("market_tier", "t3"),
                    execution.get("market_id"),
                    execution.get("asset_id"),
                ),
            )
            self.conn.execute(
                """
                INSERT INTO collateral_reservations (
                  reservation_id, decision_id, execution_id, amount_usdc, status, reason,
                  idempotency_key, created_ts_utc
                ) VALUES (?, ?, ?, ?, 'RESERVED', ?, ?, ?)
                """,
                (
                    reservation_id,
                    execution["decision_id"],
                    execution["execution_id"],
                    amount_usdc,
                    execution.get("reservation_reason") or "PRE_SUBMIT_LOCK",
                    idempotency_key,
                    created_ts_utc,
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def update_reservation_status(self, execution_id: str, status: str, reason: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE collateral_reservations
            SET status = ?, reason = COALESCE(?, reason)
            WHERE execution_id = ?
            """,
            (status, reason, execution_id),
        )
        self.conn.commit()

    def release_reservations_by_tx_hash(self, tx_hash: str, reason: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE collateral_reservations
            SET status = 'RELEASED', reason = COALESCE(?, reason)
            WHERE execution_id IN (SELECT execution_id FROM execution_records WHERE tx_hash = ?)
              AND status = 'RESERVED'
            """,
            (reason or "CHAIN_CONFIRMED", tx_hash),
        )
        self.conn.commit()

    def forfeit_reservations_by_tx_hash(self, tx_hash: str, reason: str) -> None:
        self.conn.execute(
            """
            UPDATE collateral_reservations
            SET status = 'FORFEITED', reason = ?
            WHERE execution_id IN (SELECT execution_id FROM execution_records WHERE tx_hash = ?)
              AND status = 'RESERVED'
            """,
            (reason, tx_hash),
        )
        self.conn.commit()

    def update_execution_post_submit(
        self,
        execution_id: str,
        *,
        tx_hash: str | None,
        clob_order_id: str | None,
        settlement_status: str | None = None,
        accepted: int | None = None,
        reason: str | None = None,
    ) -> None:
        sets: list[str] = []
        args: list[Any] = []
        if tx_hash is not None:
            sets.append("tx_hash = ?")
            args.append(tx_hash)
        if clob_order_id is not None:
            sets.append("clob_order_id = ?")
            args.append(clob_order_id)
        if settlement_status is not None:
            sets.append("settlement_status = ?")
            args.append(settlement_status)
        if accepted is not None:
            sets.append("accepted = ?")
            args.append(accepted)
        if reason is not None:
            sets.append("reason = ?")
            args.append(reason)
        if not sets:
            return
        args.append(execution_id)
        self.conn.execute(
            f"UPDATE execution_records SET {', '.join(sets)} WHERE execution_id = ?",
            tuple(args),
        )
        self.conn.commit()

    def update_execution_clob_result(
        self,
        execution_id: str,
        *,
        clob_order_id: str | None = None,
        tx_hash: str | None = None,
        settlement_status: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Phase 2-C-2: update execution record with CLOB result after submit_fok_order returns."""
        sets: list[str] = []
        args: list[Any] = []
        if clob_order_id is not None:
            sets.append("clob_order_id = ?")
            args.append(clob_order_id)
        if tx_hash is not None:
            sets.append("tx_hash = ?")
            args.append(tx_hash)
        if settlement_status is not None:
            sets.append("settlement_status = ?")
            args.append(settlement_status)
        if reason is not None:
            sets.append("reason = COALESCE(?, reason)")
            args.append(reason)
        if not sets:
            return
        args.append(execution_id)
        self.conn.execute(
            f"UPDATE execution_records SET {', '.join(sets)} WHERE execution_id = ?",
            tuple(args),
        )
        self.conn.commit()

    def upsert_correlation_edges(self, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """
            INSERT INTO correlation_edges (market_a, market_b, rho, window_sec, updated_ts_utc)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(market_a, market_b, window_sec) DO UPDATE SET
              rho = excluded.rho,
              updated_ts_utc = excluded.updated_ts_utc
            """,
            [
                (r["market_a"], r["market_b"], r["rho"], int(r["window_sec"]), r["updated_ts_utc"])
                for r in rows
            ],
        )
        self.conn.commit()

    def fetch_max_rho(self, market_id: str, peers: list[str], window_sec: int) -> float | None:
        if not peers:
            return None
        placeholders = ",".join("?" * len(peers))
        q = f"""
            SELECT MAX(ABS(rho)) FROM correlation_edges
            WHERE window_sec = ?
              AND (
                (market_a = ? AND market_b IN ({placeholders}))
                OR (market_b = ? AND market_a IN ({placeholders}))
              )
        """
        args: list[Any] = [window_sec, market_id, *peers, market_id, *peers]
        row = self.conn.execute(q, args).fetchone()
        if not row or row[0] is None:
            return None
        return float(row[0])

    def fetch_open_positions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT position_id, market_id, cluster_id, side, signed_notional_usd, kelly_fraction, opened_ts_utc, closed_ts_utc
            FROM positions
            WHERE closed_ts_utc IS NULL
            """
        ).fetchall()
        return [
            {
                "position_id": r[0],
                "market_id": r[1],
                "cluster_id": r[2],
                "side": r[3],
                "signed_notional_usd": float(r[4] if r[4] is not None else 0.0),
                "kelly_fraction": r[5],
                "opened_ts_utc": r[6],
                "closed_ts_utc": r[7],
            }
            for r in rows
        ]

    def append_position(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO positions (position_id, market_id, cluster_id, side, signed_notional_usd, kelly_fraction, opened_ts_utc, closed_ts_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["position_id"],
                row["market_id"],
                row["cluster_id"],
                row.get("side", "YES"),
                float(row.get("signed_notional_usd", 0.0)),
                row["kelly_fraction"],
                row["opened_ts_utc"],
                row.get("closed_ts_utc"),
            ),
        )
        self.conn.commit()

    def fetch_open_positions_extended(self) -> list[dict[str, Any]]:
        """Alias to emphasize side/signed exposure availability."""
        return self.fetch_open_positions()

    def append_paper_trade(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO paper_trades (
              paper_trade_id, decision_id, wallet_address, market_id, cluster_id, side, sizing_notional,
              kelly_fraction, cluster_delta_before, cluster_delta_after, reason, outcome, created_ts_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["paper_trade_id"],
                row["decision_id"],
                row.get("wallet_address"),
                row["market_id"],
                row["cluster_id"],
                row["side"],
                float(row["sizing_notional"]),
                float(row["kelly_fraction"]),
                row.get("cluster_delta_before"),
                row.get("cluster_delta_after"),
                row.get("reason", "DRY_RUN_PAPER_EXECUTED"),
                row.get("outcome"),
                row["created_ts_utc"],
            ),
        )
        self.conn.commit()

    def append_trade_settlement(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO realized_pnl_settlement (
              trade_id, paper_trade_id, decision_id, market_id, event_name, direction, confidence, open_reason,
              close_reason, close_condition, entry_price, exit_price, position_size_usd, estimated_ev_usd,
              realized_pnl_usd, opened_ts_utc, closed_ts_utc, source_event
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["trade_id"],
                row.get("paper_trade_id"),
                row.get("decision_id"),
                row["market_id"],
                row.get("event_name"),
                row.get("direction"),
                row.get("confidence"),
                row.get("open_reason"),
                row.get("close_reason"),
                row.get("close_condition"),
                row.get("entry_price"),
                row.get("exit_price"),
                float(row.get("position_size_usd", 0.0)),
                float(row.get("estimated_ev_usd", 0.0)),
                float(row.get("realized_pnl_usd", 0.0)),
                row["opened_ts_utc"],
                row["closed_ts_utc"],
                row.get("source_event"),
            ),
        )
        self.conn.commit()

    def fetch_top_trade_candidates(self, limit: int = 20, lookback_days: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT
              s.trade_id,
              s.market_id,
              COALESCE(s.event_name, s.market_id) AS event_name,
              COALESCE(s.direction, 'YES') AS direction,
              COALESCE(s.confidence, d.posterior_probability, 0.0) AS confidence,
              COALESCE(s.open_reason, p.reason, d.action, 'N/A') AS open_reason,
              s.entry_price,
              s.exit_price,
              s.position_size_usd,
              s.estimated_ev_usd,
              s.realized_pnl_usd,
              COALESCE(s.close_condition, s.close_reason, 'N/A') AS close_condition,
              s.opened_ts_utc,
              s.closed_ts_utc
            FROM realized_pnl_settlement s
            LEFT JOIN paper_trades p ON p.paper_trade_id = s.paper_trade_id
            LEFT JOIN strategy_decisions d ON d.decision_id = s.decision_id
            WHERE s.closed_ts_utc >= datetime('now', '-' || ? || ' day')
            ORDER BY s.closed_ts_utc DESC
            LIMIT ?
            """,
            (int(lookback_days), int(limit)),
        ).fetchall()
        return [
            {
                "trade_id": r[0],
                "market_id": r[1],
                "event_name": r[2],
                "direction": r[3],
                "confidence": float(r[4] if r[4] is not None else 0.0),
                "open_reason": r[5],
                "entry_price": r[6],
                "exit_price": r[7],
                "position_size_usd": float(r[8] if r[8] is not None else 0.0),
                "estimated_ev_usd": float(r[9] if r[9] is not None else 0.0),
                "realized_pnl_usd": float(r[10] if r[10] is not None else 0.0),
                "close_condition": r[11],
                "opened_ts_utc": r[12],
                "closed_ts_utc": r[13],
            }
            for r in rows
        ]

    def _period_filter_clause(self, period: str) -> tuple[str, tuple[Any, ...]]:
        period = period.lower()
        if period == "1d":
            return "WHERE closed_ts_utc >= datetime('now', '-1 day')", ()
        if period == "7d":
            return "WHERE closed_ts_utc >= datetime('now', '-7 day')", ()
        if period == "30d":
            return "WHERE closed_ts_utc >= datetime('now', '-30 day')", ()
        return "", ()

    def fetch_trade_list(self, limit: int = 20, status: str = "recent", period: str = "all") -> list[dict[str, Any]]:
        where_sql, args = self._period_filter_clause(period)
        status_sql = ""
        if status == "win":
            status_sql = " AND realized_pnl_usd > 0"
        elif status == "loss":
            status_sql = " AND realized_pnl_usd < 0"
        where_combined = where_sql
        if status_sql:
            where_combined = f"{where_sql}{status_sql}" if where_sql else f"WHERE 1=1{status_sql}"
        rows = self.conn.execute(
            f"""
            SELECT
              trade_id, market_id, COALESCE(event_name, market_id), COALESCE(direction, 'YES'),
              COALESCE(confidence, 0.0), COALESCE(open_reason, 'N/A'),
              entry_price, exit_price, position_size_usd, estimated_ev_usd, realized_pnl_usd,
              COALESCE(close_condition, close_reason, 'N/A'), opened_ts_utc, closed_ts_utc,
              COALESCE(source_event, 'live') as source
            FROM realized_pnl_settlement
            {where_combined}
            ORDER BY closed_ts_utc DESC
            LIMIT ?
            """,
            (*args, int(limit)),
        ).fetchall()
        return [
            {
                "trade_id": r[0],
                "market_id": r[1],
                "event_name": r[2],
                "direction": r[3],
                "confidence": float(r[4] if r[4] is not None else 0.0),
                "open_reason": r[5],
                "entry_price": r[6],
                "exit_price": r[7],
                "position_size_usd": float(r[8] if r[8] is not None else 0.0),
                "estimated_ev_usd": float(r[9] if r[9] is not None else 0.0),
                "realized_pnl_usd": float(r[10] if r[10] is not None else 0.0),
                "close_condition": r[11],
                "opened_ts_utc": r[12],
                "closed_ts_utc": r[13],
                "source": r[14] if len(r) > 14 else "live",
            }
            for r in rows
        ]

    def sync_paper_trades_to_settlement(self) -> int:
        # Phase 1: Backfill exit_price for existing rows with null exit_price
        # D64 Q2 ruling: exit price = CLOB /prices-history last price (rounded to 0/1)
        rows_to_backfill = self.conn.execute("""
            SELECT r.trade_id, r.entry_price, r.position_size_usd
            FROM realized_pnl_settlement r
            WHERE r.exit_price IS NULL
            LIMIT 50
        """).fetchall()
        backfilled = 0
        for row in rows_to_backfill:
            trade_id = row[0]
            entry_price = float(row[1]) if row[1] is not None else 0.5
            position_size = float(row[2]) if row[2] is not None else 50.0
            settlement_price = fetch_settlement_price(trade_id)
            if settlement_price is not None:
                realized_pnl = (settlement_price - entry_price) * position_size
                self.conn.execute("""
                    UPDATE realized_pnl_settlement
                    SET exit_price = ?, realized_pnl_usd = ?
                    WHERE trade_id = ? AND exit_price IS NULL
                """, (settlement_price, realized_pnl, trade_id))
                backfilled += 1

        # Phase 2: Sync new settled execution_records not yet in settlement table
        rows = self.conn.execute("""
            SELECT e.execution_id, e.market_id, e.posterior, e.ev_net, e.avg_entry_price,
                   e.qty, e.created_ts_utc
            FROM execution_records e
            WHERE e.mode = 'PAPER'
              AND e.accepted = 1
              AND e.settlement_status = 'settled'
              AND NOT EXISTS (
                  SELECT 1 FROM realized_pnl_settlement r
                  WHERE r.trade_id = e.execution_id
              )
            ORDER BY e.created_ts_utc DESC
            LIMIT 200
        """).fetchall()
        synced = 0
        for r in rows:
            execution_id = r[0]
            market_id = r[1] or "unknown"
            posterior = float(r[2] or 0.5)
            ev_net = float(r[3] or 0.0)
            entry_price = float(r[4]) if r[4] is not None else 0.5
            qty = float(r[5]) if r[5] is not None else 50.0
            created_ts = r[6]
            direction = "YES" if posterior >= 0.5 else "NO"
            event_name = self._fetch_and_cache_event_name(market_id)
            # D64 Q2: Fetch real settlement price; if unavailable, exit_price=null (do not estimate)
            settlement_price = fetch_settlement_price(execution_id)
            if settlement_price is not None:
                exit_price = settlement_price
                realized_pnl = (settlement_price - entry_price) * qty
            else:
                exit_price = None
                realized_pnl = ev_net
            self.conn.execute(
                "INSERT OR REPLACE INTO realized_pnl_settlement "
                "(trade_id, paper_trade_id, market_id, event_name, direction, confidence, "
                " open_reason, close_reason, close_condition, entry_price, exit_price, "
                " position_size_usd, estimated_ev_usd, realized_pnl_usd, "
                " opened_ts_utc, closed_ts_utc, source_event) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (execution_id, execution_id, market_id, event_name, direction,
                 posterior, "PAPER_TRADE", None, "paper_settled",
                 entry_price, exit_price, qty, ev_net, realized_pnl,
                 created_ts, created_ts, "paper")
            )
            synced += 1
        self.conn.commit()
        logger.info("[SETTLEMENT] backfilled=%d new_sync=%d", backfilled, synced)
        return backfilled + synced

    def _fetch_and_cache_event_name(self, market_id: str) -> str:
        existing = self.get_link_mapping_by_token_id(market_id)
        if existing and existing.get("event_slug"):
            return existing["event_slug"]
        slug = self.resolve_slug(market_id)
        if slug and slug != "unknown" and "..." not in slug:
            return slug
        return slug if slug else market_id[:16] + "..."

    def fetch_performance_metrics(self, period: str = "all") -> dict[str, Any]:
        where_sql, args = self._period_filter_clause(period)
        rows = self.conn.execute(
            f"""
            SELECT trade_id, closed_ts_utc, realized_pnl_usd, estimated_ev_usd
            FROM realized_pnl_settlement
            {where_sql}
            ORDER BY closed_ts_utc ASC
            """,
            args,
        ).fetchall()
        pnls = [float(r[2] if r[2] is not None else 0.0) for r in rows]
        ests = [float(r[3] if r[3] is not None else 0.0) for r in rows]
        total_pnl = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else None
        mean_pnl = (total_pnl / len(pnls)) if pnls else 0.0
        variance = (sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls)) if len(pnls) > 1 else 0.0
        std_dev = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = ((mean_pnl / std_dev) * math.sqrt(len(pnls))) if std_dev > 0 and len(pnls) > 1 else 0.0

        gross_profit = sum(p for p in pnls if p > 0)
        gross_loss = abs(sum(p for p in pnls if p < 0))
        if gross_loss == 0:
            profit_factor = gross_profit if gross_profit > 0 else None
        else:
            profit_factor = gross_profit / gross_loss

        slippage_gap = (sum((e - p) for e, p in zip(ests, pnls)) / len(pnls)) if pnls else None

        equity = 0.0
        peak = 0.0
        peak_idx = 0
        max_dd = 0.0
        trough_idx = 0
        dd_peak_idx = 0
        for idx, pnl in enumerate(pnls):
            equity += pnl
            if equity > peak:
                peak = equity
                peak_idx = idx
            if peak > 0:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
                    trough_idx = idx
                    dd_peak_idx = peak_idx
        return {
            "total_pnl_usd": float(total_pnl),
            "win_rate": win_rate,
            "sharpe_ratio": float(sharpe),
            "max_drawdown": float(max_dd),
            "peak_ts": rows[dd_peak_idx][1] if rows else None,
            "trough_ts": rows[trough_idx][1] if rows else None,
            "from_trade_id": rows[dd_peak_idx][0] if rows else None,
            "to_trade_id": rows[trough_idx][0] if rows else None,
            "profit_factor": profit_factor,
            "slippage_gap": slippage_gap,
            "trade_count": len(pnls),
        }

    def fetch_performance_history(self, period: str = "all") -> list[dict[str, Any]]:
        where_sql, args = self._period_filter_clause(period)
        rows = self.conn.execute(
            f"""
            SELECT closed_ts_utc, realized_pnl_usd
            FROM realized_pnl_settlement
            {where_sql}
            ORDER BY closed_ts_utc ASC
            """,
            args,
        ).fetchall()

        cumulative = 0.0
        points: list[dict[str, Any]] = []
        for ts, pnl in rows:
            cumulative += float(pnl if pnl is not None else 0.0)
            points.append({
                "ts": str(ts),
                "cumulative_pnl_usd": float(cumulative),
            })
        return points

    def fetch_system_status(self) -> dict[str, Any]:
        last_execution = self.conn.execute(
            """
            SELECT decision_id, accepted, reason, settlement_status, created_ts_utc
            FROM execution_records
            ORDER BY created_ts_utc DESC
            LIMIT 1
            """
        ).fetchone()
        last_trade = self.conn.execute(
            """
            SELECT trade_id, close_condition, closed_ts_utc
            FROM realized_pnl_settlement
            ORDER BY closed_ts_utc DESC
            LIMIT 1
            """
        ).fetchone()

        if not last_execution:
            return {
                "state": "idle",
                "message": "No execution records yet; waiting for strategy signal",
                "last_event_ts": last_trade[2] if last_trade else None,
                "last_decision_id": None,
                "last_execution_reason": None,
                "last_reject_reason": None,
            }

        decision_id, accepted, reason, settlement_status, created_ts_utc = last_execution
        accepted_int = int(accepted) if accepted is not None else 0
        if accepted_int == 1:
            state = "executed"
            message = f"Latest execution accepted ({settlement_status or 'submitted'})"
            reject_reason = None
        else:
            state = "rejected"
            message = f"Latest signal rejected: {reason or 'UNKNOWN_REASON'}"
            reject_reason = str(reason) if reason is not None else None

        return {
            "state": state,
            "message": message,
            "last_event_ts": str(created_ts_utc) if created_ts_utc is not None else None,
            "last_decision_id": str(decision_id) if decision_id is not None else None,
            "last_execution_reason": str(reason) if reason is not None else None,
            "last_reject_reason": reject_reason,
        }

    def upsert_link_mapping(
        self,
        *,
        market_id: str,
        token_id: str | None,
        event_slug: str | None,
        market_slug: str | None,
        canonical_event_url: str | None,
        canonical_embed_url: str | None,
        source: str,
        fetched_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO polymarket_link_map (
              market_id, token_id, event_slug, market_slug, canonical_event_url, canonical_embed_url, source, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
              token_id=excluded.token_id,
              event_slug=excluded.event_slug,
              market_slug=excluded.market_slug,
              canonical_event_url=excluded.canonical_event_url,
              canonical_embed_url=excluded.canonical_embed_url,
              source=excluded.source,
              fetched_at=excluded.fetched_at
            """,
            (
                market_id,
                token_id,
                event_slug,
                market_slug,
                canonical_event_url,
                canonical_embed_url,
                source,
                fetched_at,
            ),
        )
        self.conn.commit()

    def get_link_mapping_by_market_id(self, market_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT market_id, token_id, event_slug, market_slug, canonical_event_url, canonical_embed_url, source, fetched_at
            FROM polymarket_link_map
            WHERE market_id = ?
            LIMIT 1
            """,
            (market_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "market_id": row[0],
            "token_id": row[1],
            "event_slug": row[2],
            "market_slug": row[3],
            "canonical_event_url": row[4],
            "canonical_embed_url": row[5],
            "source": row[6],
            "fetched_at": row[7],
        }

    def get_link_mapping_by_token_id(self, token_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT market_id, token_id, event_slug, market_slug, canonical_event_url, canonical_embed_url, source, fetched_at
            FROM polymarket_link_map
            WHERE token_id = ?
            LIMIT 1
            """,
            (token_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "market_id": row[0],
            "token_id": row[1],
            "event_slug": row[2],
            "market_slug": row[3],
            "canonical_event_url": row[4],
            "canonical_embed_url": row[5],
            "source": row[6],
            "fetched_at": row[7],
        }

    def get_canonical_market_id(self, token_id: str) -> str | None:
        """
        D71 Q1 Ruling (Option A): Return COALESCE(market_id, condition_id) for a token_id.

        BTC 5m markets have market_id=NULL but condition_id populated.
        All downstream consumers must use this helper — never read market_id directly.
        """
        row = self.conn.execute(
            "SELECT COALESCE(market_id, condition_id) FROM polymarket_link_map "
            "WHERE token_id=? LIMIT 1",
            (token_id,)
        ).fetchone()
        return row[0] if row else None

    def resolve_slug(self, market_id: str | None) -> str:
        """
        Resolve human-readable slug from polymarket_link_map.
        Resolution priority:
          1. polymarket_link_map.event_slug WHERE token_id = market_id (NOCASE)
          2. First 16 chars of market_id + "..." (truncated fallback)
          3. "unknown" if market_id is None/empty
        NOTE: Returns fallback silently — NEVER raises on missing data.
        """
        if not market_id:
            return "unknown"
        try:
            row = self.conn.execute(
                "SELECT event_slug FROM polymarket_link_map "
                "WHERE token_id = ? COLLATE NOCASE LIMIT 1",
                (market_id,),
            ).fetchone()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
        return market_id[:16] + "..." if len(market_id) > 16 else market_id

    def batch_resolve_slugs(self, token_ids: list[str]) -> dict[str, str]:
        """
        D60a: Resolve slugs for multiple token_ids in ONE query.
        Returns {token_id: event_slug} for all input token_ids.
        Missing entries return empty string (caller handles fallback).
        Uses LOWER() for case-insensitive matching (same as resolve_slug's NOCASE).
        """
        if not token_ids:
            return {}
        try:
            # Use LOWER() on both sides for case-insensitive IN matching.
            # This matches resolve_slug()'s COLLATE NOCASE behavior.
            lowered_ids = [t.lower() for t in token_ids]
            placeholders = ",".join("?" * len(token_ids))
            rows = self.conn.execute(
                f"""SELECT m.token_id, COALESCE(m.event_slug, '')
                    FROM polymarket_link_map m
                    WHERE LOWER(m.token_id) IN ({placeholders})""",
                lowered_ids,
            ).fetchall()
            # Map result back to original token_ids using LOWER matching
            result = {tid: "" for tid in token_ids}
            for tok_id, slug in rows:
                for tid in token_ids:
                    if tid.lower() == tok_id.lower():
                        result[tid] = slug if slug else ""
            return result
        except Exception:
            return {tid: "" for tid in token_ids}

    def append_unresolved_link_case(
        self,
        *,
        unresolved_id: str,
        market_id: str | None,
        token_id: str | None,
        event_name: str | None,
        reason: str,
        source: str,
        created_ts_utc: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO polymarket_link_unresolved (
              unresolved_id, market_id, token_id, event_name, reason, source, created_ts_utc, resolved
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (unresolved_id, market_id, token_id, event_name, reason, source, created_ts_utc),
        )
        self.conn.commit()

    def list_open_unresolved_links(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT unresolved_id, market_id, token_id, event_name, reason, source, created_ts_utc
            FROM polymarket_link_unresolved
            WHERE resolved = 0
            ORDER BY created_ts_utc DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
        return [
            {
                "unresolved_id": r[0],
                "market_id": r[1],
                "token_id": r[2],
                "event_name": r[3],
                "reason": r[4],
                "source": r[5],
                "created_ts_utc": r[6],
            }
            for r in rows
        ]

    def mark_unresolved_link_resolved(self, unresolved_id: str) -> None:
        self.conn.execute(
            """
            UPDATE polymarket_link_unresolved
            SET resolved = 1
            WHERE unresolved_id = ?
            """,
            (unresolved_id,),
        )
        self.conn.commit()

    def link_resolver_stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM polymarket_link_map),
              (SELECT COUNT(*) FROM polymarket_link_unresolved WHERE resolved = 0),
              (SELECT COUNT(*) FROM polymarket_link_unresolved WHERE resolved = 1)
            """
        ).fetchone()
        return {
            "mappingCount": int(row[0] if row else 0),
            "unresolvedOpenCount": int(row[1] if row else 0),
            "unresolvedResolvedCount": int(row[2] if row else 0),
        }

    def fetch_readiness_metrics(self, target_trades: int = 100, target_days: int = 14) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT COUNT(*), MIN(closed_ts_utc), SUM(CASE WHEN realized_pnl_usd > 0 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN realized_pnl_usd < 0 THEN 1 ELSE 0 END)
            FROM realized_pnl_settlement
            """
        ).fetchone()
        count = int(row[0] if row and row[0] is not None else 0)
        first_closed = row[1] if row else None
        wins = int(row[2] if row and row[2] is not None else 0)
        losses = int(row[3] if row and row[3] is not None else 0)
        running_days = 0
        if first_closed:
            try:
                start = datetime.fromisoformat(str(first_closed).replace("Z", "+00:00"))
                running_days = max(0, (datetime.now(timezone.utc) - start).days)
            except ValueError:
                running_days = 0
        win_rate = (wins / (wins + losses)) if (wins + losses) > 0 else None
        is_ready = count >= target_trades and running_days >= target_days and (win_rate is not None and win_rate > 0.55)
        return {
            "current_paper_trades": count,
            "target_trades": int(target_trades),
            "running_days": int(running_days),
            "target_days": int(target_days),
            "current_win_rate": win_rate,
            "is_ready": bool(is_ready),
        }

    def write_rvf_snapshot(self, snap: dict) -> None:
        """Write a MetricsSnapshot dict to the rvf_metrics_snapshots table."""
        self.conn.execute(
            """
            INSERT INTO rvf_metrics_snapshots (
                ts_utc, ws_connected, ws_t1, ws_t2, ws_t3, ws_t5,
                trade_ticks_60s, book_events_60s, t1_window_start, t1_window_end,
                secs_remaining, t1_rollover_cnt, elapsed_since_ws,
                kyle_samples, kyle_assets, kyle_p75, kyle_last_elapsed, kyle_last_status,
                active_ew, last_cleanup_cnt, last_cleanup_ts,
                queue_depth, processed_60s, mean_p_t1, mean_p_t2, mean_z_t1, mean_z_t2,
                gate_eval_60s, gate_pass_60s, gate_abort_60s, paper_trades,
                paper_winrate, avg_ev,
                deadline_ladders, rolling_windows, total_series,
                monotone_viol, last_viol_slug, last_viol_gap, catalysts_today, oracle_high,
                readiness_kyle_pct, readiness_trades_pct, readiness_winrate_pct, readiness_all_ready
            ) VALUES (
                :ts_utc, :ws_connected, :ws_t1, :ws_t2, :ws_t3, :ws_t5,
                :trade_ticks_60s, :book_events_60s, :t1_window_start, :t1_window_end,
                :secs_remaining, :t1_rollover_cnt, :elapsed_since_ws,
                :kyle_samples, :kyle_assets, :kyle_p75, :kyle_last_elapsed, :kyle_last_status,
                :active_ew, :last_cleanup_cnt, :last_cleanup_ts,
                :queue_depth, :processed_60s, :mean_p_t1, :mean_p_t2, :mean_z_t1, :mean_z_t2,
                :gate_eval_60s, :gate_pass_60s, :gate_abort_60s, :paper_trades,
                :paper_winrate, :avg_ev,
                :deadline_ladders, :rolling_windows, :total_series,
                :monotone_viol, :last_viol_slug, :last_viol_gap, :catalysts_today, :oracle_high,
                :readiness_kyle_pct, :readiness_trades_pct, :readiness_winrate_pct, :readiness_all_ready
            )
            """,
            {
                "ts_utc": snap.get("ts_utc"),
                "ws_connected": int(snap.get("ws_connected", False)),
                "ws_t1": snap.get("ws_t1", 0),
                "ws_t2": snap.get("ws_t2", 0),
                "ws_t3": snap.get("ws_t3", 0),
                "ws_t5": snap.get("ws_t5", 0),
                "trade_ticks_60s": snap.get("trade_ticks_60s", 0),
                "book_events_60s": snap.get("book_events_60s", 0),
                "t1_window_start": snap.get("t1_window_start", 0),
                "t1_window_end": snap.get("t1_window_end", 0),
                "secs_remaining": snap.get("secs_remaining", 0.0),
                "t1_rollover_cnt": snap.get("t1_rollover_cnt", 0),
                "elapsed_since_ws": snap.get("elapsed_since_ws", 0.0),
                "kyle_samples": snap.get("kyle_samples", 0),
                "kyle_assets": snap.get("kyle_assets", 0),
                "kyle_p75": snap.get("kyle_p75", 0.0),
                "kyle_last_elapsed": snap.get("kyle_last_elapsed", 0.0),
                "kyle_last_status": snap.get("kyle_last_status", "none"),
                "active_ew": snap.get("active_ew", 0),
                "last_cleanup_cnt": snap.get("last_cleanup_cnt", 0),
                "last_cleanup_ts": snap.get("last_cleanup_ts", 0.0),
                "queue_depth": snap.get("queue_depth", 0),
                "processed_60s": snap.get("processed_60s", 0),
                "mean_p_t1": snap.get("mean_p_t1", 0.0),
                "mean_p_t2": snap.get("mean_p_t2", 0.0),
                "mean_z_t1": snap.get("mean_z_t1", 0.0),
                "mean_z_t2": snap.get("mean_z_t2", 0.0),
                "gate_eval_60s": snap.get("gate_eval_60s", 0),
                "gate_pass_60s": snap.get("gate_pass_60s", 0),
                "gate_abort_60s": snap.get("gate_abort_60s", 0),
                "paper_trades": snap.get("paper_trades", 0),
                "paper_winrate": snap.get("paper_winrate", 0.0),
                "avg_ev": snap.get("avg_ev", 0.0),
                "deadline_ladders": snap.get("deadline_ladders", 0),
                "rolling_windows": snap.get("rolling_windows", 0),
                "total_series": snap.get("total_series", 0),
                "monotone_viol": snap.get("monotone_viol", 0),
                "last_viol_slug": snap.get("last_viol_slug", ""),
                "last_viol_gap": snap.get("last_viol_gap", 0.0),
                "catalysts_today": snap.get("catalysts_today", 0),
                "oracle_high": snap.get("oracle_high", 0),
                "readiness_kyle_pct": snap.get("readiness_kyle_pct", 0.0),
                "readiness_trades_pct": snap.get("readiness_trades_pct", 0.0),
                "readiness_winrate_pct": snap.get("readiness_winrate_pct", 0.0),
                "readiness_all_ready": int(snap.get("readiness_all_ready", False)),
            },
        )
        self.conn.commit()

    def fetch_watched_wallets_by_label(self, label: str, *, active_only: bool = True) -> list[str]:
        if active_only:
            rows = self.conn.execute(
                "SELECT address FROM watched_wallets WHERE label = ? AND active = 1 ORDER BY created_ts_utc",
                (label,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT address FROM watched_wallets WHERE label = ? ORDER BY created_ts_utc",
                (label,),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def upsert_watched_wallet(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO watched_wallets (address, label, source, created_ts_utc, active)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
              label=excluded.label,
              source=excluded.source,
              active=excluded.active
            """,
            (
                row["address"].lower(),
                row.get("label"),
                row.get("source", "manual"),
                row["created_ts_utc"],
                int(row.get("active", 1)),
            ),
        )
        self.conn.commit()

    def flush_wallet_obs_buffer(self) -> None:
        """Force-flush wallet_observations buffer. Call from heartbeat."""
        with self._flush_lock:
            if self._wallet_obs_buffer:
                try:
                    self.conn.executemany(
                        """
                        INSERT INTO wallet_observations (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc, transaction_hash, order_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                row["obs_id"],
                                row["address"].lower(),
                                row.get("market_id"),
                                row["obs_type"],
                                row["payload_json"] if isinstance(row["payload_json"], str) else json.dumps(row["payload_json"], ensure_ascii=False),
                                row["ingest_ts_utc"],
                                row.get("transaction_hash"),
                                row.get("order_id"),
                            )
                            for row in self._wallet_obs_buffer
                        ],
                    )
                    self.conn.commit()
                except Exception as exc:
                    logger.warning("[DB][WALLET_OBS_FLUSH_ERR] %s", exc)
                finally:
                    self._wallet_obs_buffer.clear()

    def flush_kyle_buffer(self) -> None:
        """Force-flush kyle_lambda_samples buffer. Call from heartbeat."""
        with self._flush_lock:
            if self._kyle_buffer:
                rows_to_insert = [
                    (row["asset_id"], normalize_external_ts_to_utc(row.get("ts_utc")),
                     float(row["delta_price"]), float(row["trade_size"]),
                     float(row["lambda_obs"]), row.get("market_id"),
                     row.get("source", "standalone"),
                     row.get("created_at"), row.get("window_ts", 0))
                    for row in self._kyle_buffer
                    if float(row.get("trade_size") or 0) > 0
                    and float(row.get("lambda_obs") or 0) > 0
                ]
                if rows_to_insert:
                    try:
                        self.conn.executemany(
                            """
                            INSERT INTO kyle_lambda_samples (
                                asset_id, ts_utc, delta_price, trade_size,
                                lambda_obs, market_id, source, created_at, window_ts
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)
                            """,
                            rows_to_insert,
                        )
                        self.conn.commit()
                    except Exception as exc:
                        logger.warning("[DB][KYLE_FLUSH_ERR] %s", exc)
                self._kyle_buffer.clear()

    def append_wallet_observation(self, row: dict[str, Any]) -> None:
        self._wallet_obs_buffer.append(row)
        if len(self._wallet_obs_buffer) >= self._BATCH_SIZE:
            with self._flush_lock:
                self._flush_wallet_obs_buffer()

    def append_kyle_lambda_sample(self, row: dict[str, Any]) -> None:
        """
        Buffer-first kyle λ sample ingestion.

        Guards (入口層唯一防線):
          - trade_size <= 0  → silent discard（防止 ZeroDivisionError 與 P75 污染）
          - lambda_obs <= 0  → silent discard（防止 P75 systematic underestimate）

        Buffered: flushes via _flush_kyle_buffer() every _BATCH_SIZE rows or on heartbeat.

        ⚠️ 禁止在此方法中呼叫 self.conn.execute() — 所有 DB 寫入必須通過 _flush_kyle_buffer()
        ⚠️ 禁止在 _flush_kyle_buffer() 中移除 filter layer — 保留作雙重防禦
        """
        trade_size = float(row.get("trade_size") or 0)
        if trade_size <= 0:
            return  # guard A
        lambda_obs = float(row.get("lambda_obs") or 0)
        if lambda_obs <= 0:
            return  # guard B

        self._kyle_buffer.append(row)
        if len(self._kyle_buffer) >= self._BATCH_SIZE:
            with self._flush_lock:
                self._flush_kyle_buffer()

    def _flush_wallet_obs_buffer(self) -> None:
        if not self._wallet_obs_buffer:
            return
        try:
            self.conn.executemany(
                """
                INSERT INTO wallet_observations (obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc, transaction_hash, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["obs_id"],
                        row["address"].lower(),
                        row.get("market_id"),
                        row["obs_type"],
                        row["payload_json"] if isinstance(row["payload_json"], str) else json.dumps(row["payload_json"], ensure_ascii=False),
                        row["ingest_ts_utc"],
                        row.get("transaction_hash"),
                        row.get("order_id"),
                    )
                    for row in self._wallet_obs_buffer
                ],
            )
            self.conn.commit()
        except Exception as exc:
            logger.warning("[DB][WALLET_OBS_FLUSH_ERR] %s", exc)
        finally:
            self._wallet_obs_buffer.clear()

    def _flush_kyle_buffer(self) -> None:
        if not self._kyle_buffer:
            return

        total_in_buffer = len(self._kyle_buffer)  # ← D100: guard bypass monitoring

        rows_to_insert = [
            (row["asset_id"], normalize_external_ts_to_utc(row.get("ts_utc")),
             float(row["delta_price"]), float(row["trade_size"]),
             float(row["lambda_obs"]), row.get("market_id"),
             row.get("source", "standalone"),
             row.get("created_at"), row.get("window_ts", 0))
            for row in self._kyle_buffer
            if float(row.get("trade_size") or 0) > 0
            and float(row.get("lambda_obs") or 0) > 0
        ]

        # ← D100: guard bypass monitoring
        filtered_count = total_in_buffer - len(rows_to_insert)
        if filtered_count > 0:
            logger.warning(
                "[DB][KYLE_FLUSH] guard bypass detected: %d/%d rows filtered at flush — "
                "check append_kyle_lambda_sample guards (TASK-D100-1)",
                filtered_count, total_in_buffer,
            )

        if rows_to_insert:
            try:
                self.conn.executemany(
                    """
                    INSERT INTO kyle_lambda_samples (
                        asset_id, ts_utc, delta_price, trade_size,
                        lambda_obs, market_id, source, created_at, window_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')), ?)
                    """,
                    rows_to_insert,
                )
                self.conn.commit()  # ← 一次，在 executemany 之後
            except Exception as exc:
                logger.warning("[DB][KYLE_FLUSH_ERR] %s", exc)
                # ⚠️ 禁止 raise — flush 路徑只能 log.warning
        self._kyle_buffer.clear()

    def append_insider_score_snapshot(self, row: dict[str, Any]) -> None:
        with self._snapshot_lock:
            self.conn.execute(
                """
                INSERT INTO insider_score_snapshots (score_id, address, market_id, score, reasons_json, ingest_ts_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["score_id"],
                    row["address"].lower(),
                    row.get("market_id"),
                    float(row["score"]),
                    row["reasons_json"] if isinstance(row["reasons_json"], str) else json.dumps(row["reasons_json"], ensure_ascii=False),
                    row["ingest_ts_utc"],
                ),
            )
            self.conn.commit()

    def count_raw_events_by_layer(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT layer, COUNT(*) FROM raw_events GROUP BY layer"
        ).fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    def count_execution_accepted(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM execution_records WHERE accepted = 1"
        ).fetchone()
        return int(row[0] if row else 0)

    def fetch_active_watched_addresses(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT address FROM watched_wallets WHERE active = 1 ORDER BY created_ts_utc"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def fetch_distinct_trade_wallets(self, limit: int = 40) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT address FROM wallet_observations
            WHERE obs_type = 'clob_trade'
              AND address != '0x0000000000000000000000000000000000000000'
            GROUP BY address
            ORDER BY MAX(ingest_ts_utc) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [str(r[0]) for r in rows]

    def fetch_recent_wallet_observations(self, address: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT obs_id, address, market_id, obs_type, payload_json, ingest_ts_utc
            FROM wallet_observations
            WHERE address = ?
            ORDER BY ingest_ts_utc DESC
            LIMIT ?
            """,
            (address.lower(), limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                payload = json.loads(r[4])
            except json.JSONDecodeError:
                payload = {}
            out.append(
                {
                    "obs_id": r[0],
                    "address": r[1],
                    "market_id": r[2],
                    "obs_type": r[3],
                    "payload": payload,
                    "ingest_ts_utc": r[5],
                }
            )
        return out

    def fetch_discovered_entity(self, entity_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT entity_id, trust_score, primary_tag, sample_size, last_updated_at
            FROM discovered_entities
            WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "entity_id": row[0],
            "trust_score": float(row[1]),
            "primary_tag": row[2],
            "sample_size": int(row[3]),
            "last_updated_at": row[4],
        }

    def count_tier1_entities(
        self,
        *,
        trust_threshold: float = 70.0,
        tags: tuple[str, ...] = ("ALGO_SLICING", "SMART_MONEY_QUANT"),
    ) -> int:
        placeholders = ",".join("?" for _ in tags)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM discovered_entities
            WHERE trust_score >= ?
              AND primary_tag IN ({placeholders})
            """,
            (float(trust_threshold), *tags),
        ).fetchone()
        return int(row[0] if row else 0)

    def count_tier1_entities_since(
        self,
        since_ts_utc: str,
        *,
        trust_threshold: float = 70.0,
        tags: tuple[str, ...] = ("ALGO_SLICING", "SMART_MONEY_QUANT"),
    ) -> int:
        placeholders = ",".join("?" for _ in tags)
        row = self.conn.execute(
            f"""
            SELECT COUNT(*)
            FROM discovered_entities
            WHERE trust_score >= ?
              AND primary_tag IN ({placeholders})
              AND last_updated_at >= ?
            """,
            (float(trust_threshold), *tags, str(since_ts_utc)),
        ).fetchone()
        return int(row[0] if row else 0)

    # -------------------------------------------------------------------------
    # Signal engine: entropy signals
    # -------------------------------------------------------------------------

    def append_pending_entropy_signal(self, row: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO pending_entropy_signals (
              signal_id, market_id, token_id, entropy_z, sim_pnl_proxy,
              trigger_address, trigger_ts_utc, created_ts_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))
            """,
            (
                row["signal_id"],
                row["market_id"],
                row.get("token_id"),
                float(row["entropy_z"]),
                row.get("sim_pnl_proxy"),
                row["trigger_address"].lower(),
                row["trigger_ts_utc"],
                row.get("created_ts_utc"),
            ),
        )
        self.conn.commit()

    def get_kyle_lambda_p75(self, asset_id: str, days: int = 7) -> float | None:
        """
        Returns the P75 kyle_lambda for the given asset_id over the last `days` days.
        Returns None if fewer than 10 samples are available.
        Uses P75 (not mean) to be robust against extreme spikes in low-liquidity markets.
        """
        rows = self.conn.execute(
            """
            SELECT lambda_obs FROM kyle_lambda_samples
            WHERE asset_id = ?
            AND ts_utc > datetime('now', ? || ' days')
            ORDER BY lambda_obs
            """,
            (asset_id, f"-{days}"),
        ).fetchall()
        if len(rows) < 10:
            return None
        p75_idx = int(len(rows) * 0.75)
        return rows[p75_idx][0]

    def get_kyle_lambda_global_p75(self, days: int = 30) -> float | None:
        """
        Returns the cross-asset global P75 kyle_lambda across all asset_ids.

        Used as fallback for asset_ids with insufficient local samples (< 10).
        Returns None if fewer than 10 distinct asset_ids have samples
        (insufficient market diversity for a representative global fallback).
        """
        rows = self.conn.execute(
            """
            SELECT lambda_obs FROM kyle_lambda_samples
            WHERE ts_utc > datetime('now', ? || ' days')
            ORDER BY lambda_obs
            """,
            (f"-{days}",),
        ).fetchall()
        if len(rows) < 10:
            return None

        # Check distinct asset diversity
        asset_rows = self.conn.execute(
            """
            SELECT COUNT(DISTINCT asset_id) FROM kyle_lambda_samples
            WHERE ts_utc > datetime('now', ? || ' days')
            """,
            (f"-{days}",),
        ).fetchone()
        distinct_assets = asset_rows[0] if asset_rows else 0
        if distinct_assets < 3:
            return None  # Not enough market diversity for global fallback

        p75_idx = int(len(rows) * 0.75)
        return rows[p75_idx][0]

    def fetch_unconsumed_entropy_signals(
        self, market_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if market_id is not None:
            rows = self.conn.execute(
                """
                SELECT signal_id, market_id, token_id, entropy_z, sim_pnl_proxy,
                       trigger_address, trigger_ts_utc, consumed_at, consumed_by, created_ts_utc
                FROM pending_entropy_signals
                WHERE consumed_at IS NULL AND market_id = ?
                ORDER BY created_ts_utc ASC
                LIMIT ?
                """,
                (market_id, int(limit)),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT signal_id, market_id, token_id, entropy_z, sim_pnl_proxy,
                       trigger_address, trigger_ts_utc, consumed_at, consumed_by, created_ts_utc
                FROM pending_entropy_signals
                WHERE consumed_at IS NULL
                ORDER BY created_ts_utc ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [
            {
                "signal_id": r[0],
                "market_id": r[1],
                "token_id": r[2],
                "entropy_z": float(r[3]),
                "sim_pnl_proxy": r[4],
                "trigger_address": r[5],
                "trigger_ts_utc": r[6],
                "consumed_at": r[7],
                "consumed_by": r[8],
                "created_ts_utc": r[9],
            }
            for r in rows
        ]

    def mark_entropy_signal_consumed(self, signal_id: str, consumed_by: str) -> None:
        self.conn.execute(
            """
            UPDATE pending_entropy_signals
            SET consumed_at = datetime('now'), consumed_by = ?
            WHERE signal_id = ?
            """,
            (consumed_by, signal_id),
        )
        self.conn.commit()

    # -------------------------------------------------------------------------
    # Signal engine: wallet market positions (LIFO accounting)
    # -------------------------------------------------------------------------

    def upsert_wallet_market_position_lifo(
        self,
        wallet_address: str,
        market_id: str,
        fill_price: float,
        fill_qty: float,
        side: str,
        updated_ts_utc: str,
    ) -> None:
        side = side.upper()
        wallet_address = wallet_address.lower()

        if side not in ("BUY", "SELL"):
            return

        # Fetch current position
        row = self.conn.execute(
            """
            SELECT current_position_notional, avg_entry_price
            FROM wallet_market_positions
            WHERE wallet_address = ? AND market_id = ?
            """,
            (wallet_address, market_id),
        ).fetchone()

        if row is None:
            # New position: only BUY can create
            if side == "SELL":
                return  # can't sell what you don't have
            self.conn.execute(
                """
                INSERT INTO wallet_market_positions
                  (wallet_address, market_id, current_position_notional, avg_entry_price, last_updated_ts_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (wallet_address, market_id, float(fill_qty), float(fill_price), updated_ts_utc),
            )
            self.conn.commit()
            return

        current_notional = float(row[0])
        avg_entry = float(row[1])

        if side == "BUY":
            # Increase position, update avg_entry_price via VWAP
            new_notional = current_notional + fill_qty
            if new_notional > 0:
                new_avg = (avg_entry * current_notional + fill_price * fill_qty) / new_notional
            else:
                new_avg = 0.0
            self.conn.execute(
                """
                INSERT INTO wallet_market_positions
                  (wallet_address, market_id, current_position_notional, avg_entry_price, last_updated_ts_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, market_id) DO UPDATE SET
                  current_position_notional = excluded.current_position_notional,
                  avg_entry_price = excluded.avg_entry_price,
                  last_updated_ts_utc = excluded.last_updated_ts_utc
                """,
                (wallet_address, market_id, new_notional, new_avg, updated_ts_utc),
            )
            self.conn.commit()

        elif side == "SELL":
            # LIFO: reduce position, do NOT update avg_entry_price
            if current_notional <= 0:
                return  # nothing to sell
            new_notional = current_notional - fill_qty
            if new_notional < 0:
                new_notional = 0.0
            # avg_entry_price stays the same (LIFO: sold from most recent buy)
            self.conn.execute(
                """
                INSERT INTO wallet_market_positions
                  (wallet_address, market_id, current_position_notional, avg_entry_price, last_updated_ts_utc)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(wallet_address, market_id) DO UPDATE SET
                  current_position_notional = excluded.current_position_notional,
                  avg_entry_price = excluded.avg_entry_price,
                  last_updated_ts_utc = excluded.last_updated_ts_utc
                """,
                (wallet_address, market_id, new_notional, avg_entry, updated_ts_utc),
            )
            self.conn.commit()

    def get_wallet_market_position(
        self, wallet_address: str, market_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT wallet_address, market_id, current_position_notional, avg_entry_price, last_updated_ts_utc
            FROM wallet_market_positions
            WHERE wallet_address = ? AND market_id = ?
            """,
            (wallet_address.lower(), market_id),
        ).fetchone()
        if not row:
            return None
        return {
            "wallet_address": row[0],
            "market_id": row[1],
            "current_position_notional": float(row[2]),
            "avg_entry_price": float(row[3]),
            "last_updated_ts_utc": row[4],
        }

    # -------------------------------------------------------------------------
    # Insider pattern flags (FORENSIC ONLY — Invariant 6.2)
    # -------------------------------------------------------------------------

    def insert_insider_pattern_flag(self, **kwargs: Any) -> int:
        """
        Insert a single insider_pattern_flags row.
        Returns the inserted row id.
        """
        self.conn.execute(
            """
            INSERT INTO insider_pattern_flags (
              wallet_address, asset_id, detected_ts_utc, case_type,
              account_age_days, prior_at_bet, stake_usd,
              correlated_mkts, cluster_wallet_count, same_ts_wallets,
              has_decoy_bets, pattern_score, flag_reason, human_reviewed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(kwargs.get("wallet_address", "")).lower(),
                str(kwargs.get("asset_id", "")),
                str(kwargs.get("detected_ts_utc", "")),
                str(kwargs.get("case_type", "SOLO_OP")),
                kwargs.get("account_age_days"),
                kwargs.get("prior_at_bet"),
                kwargs.get("stake_usd"),
                int(kwargs.get("correlated_mkts", 0)),
                int(kwargs.get("cluster_wallet_count", 1)),
                int(kwargs.get("same_ts_wallets", 0)),
                int(kwargs.get("has_decoy_bets", 0)),
                float(kwargs.get("pattern_score", 0.0)),
                str(kwargs.get("flag_reason", "")),
                int(kwargs.get("human_reviewed", 0)),
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT last_insert_rowid()").fetchone()
        return int(row[0] if row else 0)

    def get_unreviewed_flags(self, min_score: float = 0.70) -> list[dict[str, Any]]:
        """
        Return all unreviewed insider pattern flags with pattern_score >= min_score,
        ordered by score descending.
        """
        rows = self.conn.execute(
            """
            SELECT id, wallet_address, asset_id, detected_ts_utc, case_type,
                   account_age_days, prior_at_bet, stake_usd,
                   correlated_mkts, cluster_wallet_count, same_ts_wallets,
                   has_decoy_bets, pattern_score, flag_reason, human_reviewed,
                   created_ts_utc
            FROM insider_pattern_flags
            WHERE human_reviewed = 0 AND pattern_score >= ?
            ORDER BY pattern_score DESC
            """,
            (float(min_score),),
        ).fetchall()
        return [
            {
                "id": r[0],
                "wallet_address": r[1],
                "asset_id": r[2],
                "detected_ts_utc": r[3],
                "case_type": r[4],
                "account_age_days": r[5],
                "prior_at_bet": r[6],
                "stake_usd": r[7],
                "correlated_mkts": r[8],
                "cluster_wallet_count": r[9],
                "same_ts_wallets": r[10],
                "has_decoy_bets": bool(r[11]),
                "pattern_score": float(r[12]) if r[12] is not None else 0.0,
                "flag_reason": r[13],
                "human_reviewed": bool(r[14]),
                "created_ts_utc": r[15],
            }
            for r in rows
        ]

    def mark_flag_reviewed(self, flag_id: int) -> None:
        """Mark a single insider pattern flag as human_reviewed."""
        self.conn.execute(
            "UPDATE insider_pattern_flags SET human_reviewed = 1 WHERE id = ?",
            (int(flag_id),),
        )
        self.conn.commit()

    def get_wallet_first_seen(self, wallet_address: str) -> str | None:
        """
        Return the earliest ingest_ts_utc for a wallet across wallet_observations.
        Used by run_radar.py pattern scoring hook.
        """
        row = self.conn.execute(
            """
            SELECT MIN(ingest_ts_utc) FROM wallet_observations WHERE address = ?
            """,
            (wallet_address.lower(),),
        ).fetchone()
        return str(row[0]) if row and row[0] else None


class AsyncDBWriter:
    """Async DB queue to keep trading path non-blocking."""

    def __init__(self, db: ShadowDB) -> None:
        self.db = db
        self._q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Signal the thread to stop accepting new work, then drain any pending
        # items from the queue before joining the thread.
        self._running = False
        self._q.join()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        # Flush buffers to ensure all queued writes have landed in the DB
        # before the caller receives control.
        self.db.flush_wallet_obs_buffer()
        self.db.flush_kyle_buffer()

    def submit(self, kind: str, payload: dict[str, Any]) -> None:
        self._q.put((kind, payload))

    def _loop(self) -> None:
        while self._running:
            try:
                kind, payload = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if kind == "raw":
                self.db.append_raw_event(payload)
                self._q.task_done()
            elif kind == "decision":
                self.db.append_strategy_decision(payload)
                self._q.task_done()
            elif kind == "execution":
                self.db.append_execution_record(payload)
                self._q.task_done()
            elif kind == "pending_chain":
                self.db.upsert_pending_chain(
                    payload["tx_hash"],
                    int(payload["required_confirmations"]),
                    str(payload["status"]),
                    int(payload["confirmations"]),
                    str(payload["updated_ts_utc"]),
                )
                self._q.task_done()
            elif kind == "settlement_update":
                self.db.update_execution_settlement(
                    str(payload["tx_hash"]),
                    int(payload["confirmations"]),
                    str(payload["status"]),
                    mined_block_hash=payload.get("mined_block_hash"),
                )
                self._q.task_done()
            elif kind == "position":
                self.db.append_position(payload)
                self._q.task_done()
            elif kind == "atomic_execution_reserve":
                self.db.atomic_execution_and_reserve(
                    execution=payload["execution"],
                    reservation_id=str(payload["reservation_id"]),
                    amount_usdc=float(payload["amount_usdc"]),
                    idempotency_key=payload.get("idempotency_key"),
                    created_ts_utc=str(payload["created_ts_utc"]),
                )
                self._q.task_done()
            elif kind == "reservation_release_tx":
                self.db.release_reservations_by_tx_hash(
                    str(payload["tx_hash"]),
                    payload.get("reason"),
                )
                self._q.task_done()
            elif kind == "reservation_forfeit_tx":
                self.db.forfeit_reservations_by_tx_hash(str(payload["tx_hash"]), str(payload["reason"]))
                self._q.task_done()
            elif kind == "correlation_upsert":
                self.db.upsert_correlation_edges(list(payload["rows"]))
                self._q.task_done()
            elif kind == "execution_post_submit":
                self.db.update_execution_post_submit(
                    str(payload["execution_id"]),
                    tx_hash=payload.get("tx_hash"),
                    clob_order_id=payload.get("clob_order_id"),
                    settlement_status=payload.get("settlement_status"),
                    accepted=payload.get("accepted"),
                    reason=payload.get("reason"),
                )
                self._q.task_done()
            elif kind == "watched_wallet":
                self.db.upsert_watched_wallet(payload)
                self._q.task_done()
            elif kind == "wallet_observation":
                self.db.append_wallet_observation(payload)
                self._q.task_done()
            elif kind == "insider_score":
                self.db.append_insider_score_snapshot(payload)
                self._q.task_done()
            elif kind == "hunting_shadow_hit":
                self.db.append_hunting_shadow_hit(payload)
                self._q.task_done()
            elif kind == "virtual_entity_event":
                self.db.append_virtual_entity_event(payload)
                self._q.task_done()
            elif kind == "discovered_entity_upsert":
                self.db.upsert_discovered_entity(payload)
                self._q.task_done()
            elif kind == "tracked_wallet_upsert":
                self.db.upsert_tracked_wallet(payload)
                self._q.task_done()
            elif kind == "discovery_audit":
                self.db.append_discovery_audit(payload)
                self._q.task_done()
            elif kind == "paper_trade":
                self.db.append_paper_trade(payload)
                self._q.task_done()
            elif kind == "trade_settlement":
                self.db.append_trade_settlement(payload)
                self._q.task_done()
            elif kind == "pending_entropy_signal":
                self.db.append_pending_entropy_signal(payload)
                self._q.task_done()
            elif kind == "wallet_market_position_lifo":
                self.db.upsert_wallet_market_position_lifo(
                    wallet_address=payload["wallet_address"],
                    market_id=payload["market_id"],
                    fill_price=float(payload["fill_price"]),
                    fill_qty=float(payload["fill_qty"]),
                    side=str(payload["side"]),
                    updated_ts_utc=str(payload["updated_ts_utc"]),
                )
                self._q.task_done()



