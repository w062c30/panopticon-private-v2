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
  FOREIGN KEY(decision_id) REFERENCES strategy_decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS positions (
  position_id TEXT PRIMARY KEY,
  market_id TEXT NOT NULL,
  cluster_id TEXT NOT NULL,
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
  ingest_ts_utc TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id TEXT PRIMARY KEY,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  before_json TEXT,
  after_json TEXT,
  reason TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL
);
