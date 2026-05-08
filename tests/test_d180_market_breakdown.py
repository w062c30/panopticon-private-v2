"""D180: build_market_breakdown aggregation (synthetic SQLite)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from panopticon_py.diagnostics.market_breakdown import build_market_breakdown


def _bootstrap_minimal_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE hunting_shadow_hits (
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
        CREATE TABLE kyle_lambda_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id TEXT NOT NULL,
            ts_utc TEXT NOT NULL,
            delta_price REAL NOT NULL,
            trade_size REAL NOT NULL,
            lambda_obs REAL NOT NULL,
            market_id TEXT,
            source TEXT DEFAULT 'standalone',
            created_at TEXT NOT NULL,
            window_ts INTEGER DEFAULT 0
        );
        CREATE TABLE polymarket_link_map (
            token_id TEXT,
            event_slug TEXT,
            market_slug TEXT
        );
        """
    )
    conn.commit()


def test_market_breakdown_joins_hits_and_kyle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dbp = tmp_path / "t.db"
    conn = sqlite3.connect(str(dbp))
    _bootstrap_minimal_schema(conn)
    conn.execute(
        """INSERT INTO hunting_shadow_hits VALUES
        ('h1','0x1','tokA',0,-6.0,-0.1,NULL,'{}','2026-01-01T00:00:00.000Z'),
        ('h2','0x1','tokA',0,-4.5,-0.2,NULL,'{}','2026-01-02T00:00:00.000Z')
        """
    )
    conn.execute(
        """INSERT INTO kyle_lambda_samples
        (asset_id, ts_utc, delta_price, trade_size, lambda_obs, created_at)
        VALUES ('tokA','2026-01-01T00:00:00.000Z',0.01,10,0.001,'2026-01-01T00:00:00.000Z')
        """
    )
    conn.execute(
        "INSERT INTO polymarket_link_map VALUES ('tokA','Will X happen?','slug-x')"
    )
    conn.commit()
    conn.close()

    es = tmp_path / "entropy_status.json"
    es.write_text(
        json.dumps(
            {
                "tokens": {
                    "tokA": {
                        "events": 5,
                        "h_hist": 10,
                        "trigger_locked": False,
                        "z_ready": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PANOPTICON_DB_PATH", str(dbp))
    monkeypatch.setenv("ENTROPY_STATUS_PATH", str(es))

    out = build_market_breakdown(limit=10, sort_key="abs_z")
    assert out["rows"]
    r0 = out["rows"][0]
    assert r0["market_id"] == "tokA"
    assert r0["question"] == "Will X happen?"
    assert r0["abs_z_max"] == pytest.approx(6.0)
    assert r0["fire_count"] == 2
    assert r0["kyle_n"] == 1
    assert r0["events"] == 5
    assert r0["z_ready"] is True
    assert r0["ev_count"] == 5
    assert r0["h_count"] == 10
    assert out["summary"]["entropy_tokens_loaded"] >= 1
    assert out["summary"]["entropy_load_error"] is None


def test_market_breakdown_entropy_key_normalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dbp = tmp_path / "t.db"
    conn = sqlite3.connect(str(dbp))
    _bootstrap_minimal_schema(conn)
    conn.execute(
        """INSERT INTO hunting_shadow_hits VALUES
        ('h1','0x1','0xAbC123',0,-5.0,-0.1,NULL,'{}','2026-01-01T00:00:00.000Z')
        """
    )
    conn.commit()
    conn.close()

    # D181b: support direct-root entropy format and case/0x normalization.
    es = tmp_path / "entropy_status.json"
    es.write_text(
        json.dumps(
            {
                "abc123": {
                    "events": 7,
                    "h_hist": 9,
                    "trigger_locked": True,
                    "z_ready": False,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PANOPTICON_DB_PATH", str(dbp))
    monkeypatch.setenv("ENTROPY_STATUS_PATH", str(es))

    out = build_market_breakdown(limit=10, sort_key="abs_z")
    r0 = out["rows"][0]
    assert r0["market_id"] == "0xAbC123"
    assert r0["events"] == 7
    assert r0["h_hist"] == 9
    assert r0["locked"] is True
    assert r0["z_ready"] is False
