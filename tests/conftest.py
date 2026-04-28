"""
conftest.py — shared pytest fixtures for panopticon test suite.
"""
import pytest
import sqlite3
from unittest.mock import patch
from panopticon_py.db import ShadowDB


# ── Global mock: fetch_best_ask ──────────────────────────────────────────────
# D64: _process_event now calls fetch_best_ask() to get entry price.
# This session-scoped autouse fixture prevents 12 signal_engine tests from
# breaking when they don't mock the new dependency.
@pytest.fixture(autouse=True)
def mock_fetch_best_ask():
    with patch("panopticon_py.signal_engine.fetch_best_ask", return_value=0.50):
        yield


@pytest.fixture
def shadow_db(tmp_path):
    """
    Provides a ShadowDB instance with full schema (bootstrap called).
    Use this fixture for tests that need polymarket_link_map, discovered_entities,
    wallet_observations, or any other schema table.
    """
    db_path = str(tmp_path / "test.db")
    db = ShadowDB(db_path)
    db.bootstrap()
    yield db
    db.close()


@pytest.fixture
def schema_db(tmp_path):
    """
    Provides a raw SQLite connection with correct discovered_entities schema
    (with primary_tag) and polymarket_link_map.
    Use for MetricsCollector tests that create their own FakeDB wrapper.
    """
    db_path = str(tmp_path / "schema_test.db")
    conn = sqlite3.connect(db_path)
    # discovered_entities with primary_tag (needed by sync_consensus_from_db)
    conn.execute("""
        CREATE TABLE discovered_entities (
            entity_id TEXT PRIMARY KEY,
            insider_score REAL NOT NULL,
            address TEXT,
            primary_tag TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE wallet_observations (
            obs_id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            market_id TEXT NOT NULL,
            obs_type TEXT NOT NULL,
            ingest_ts_utc TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE polymarket_link_map (
            market_id TEXT PRIMARY KEY,
            token_id TEXT,
            event_slug TEXT,
            market_slug TEXT,
            canonical_event_url TEXT,
            canonical_embed_url TEXT,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_poly_link_token ON polymarket_link_map(token_id)")
    conn.commit()
    yield conn
    conn.close()
