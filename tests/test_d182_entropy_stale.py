"""D182a: entropy_snapshot_stale annotation in build_market_breakdown summary."""
from __future__ import annotations

import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import panopticon_py.diagnostics.market_breakdown as mbm


def _write_entropy_fixture(tmp_path: Path, total: int = 0, z_ready: int = 0, age_sec: int = 10) -> str:
    p = tmp_path / "entropy_status.json"
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    p.write_text(
        json.dumps(
            {
                "updated_ts": ts,
                "total": total,
                "z_ready_count": z_ready,
                "locked_count": 0,
                "tokens": {},
            }
        ),
        encoding="utf-8",
    )
    return str(p)


def _make_minimal_db(tmp_path: Path) -> str:
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS hunting_shadow_hits (
            market_id TEXT, entropy_z REAL, sim_pnl_proxy REAL, created_ts_utc TEXT
        );
        CREATE TABLE IF NOT EXISTS kyle_lambda_samples (
            asset_id TEXT, lambda_obs REAL
        );
        CREATE TABLE IF NOT EXISTS polymarket_link_map (
            token_id TEXT, event_slug TEXT, market_slug TEXT
        );
        """
    )
    conn.commit()
    conn.close()
    return str(db)


def test_stale_true_when_total_zero(tmp_path: Path) -> None:
    ep = _write_entropy_fixture(tmp_path, total=0, z_ready=0, age_sec=5)
    db = _make_minimal_db(tmp_path)
    result = mbm.build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_snapshot_stale"] is True


def test_stale_false_when_tokens_present_and_fresh(tmp_path: Path) -> None:
    db_path = _make_minimal_db(tmp_path)
    ep = tmp_path / "entropy_status.json"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    tokens = {
        "0xabc123": {
            "h_hist": 10,
            "need": 5,
            "trigger_locked": False,
            "events": 20,
            "healthy_span": 300.0,
            "z_ready": True,
        }
    }
    ep.write_text(
        json.dumps(
            {
                "updated_ts": ts,
                "total": 1,
                "z_ready_count": 1,
                "locked_count": 0,
                "tokens": tokens,
            }
        ),
        encoding="utf-8",
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO hunting_shadow_hits VALUES ('0xabc123', 2.5, 0.1, '2026-05-09T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    result = mbm.build_market_breakdown(db_path=db_path, entropy_path=str(ep))
    assert result["summary"]["entropy_snapshot_stale"] is False


def test_stale_true_when_file_missing(tmp_path: Path) -> None:
    db = _make_minimal_db(tmp_path)
    result = mbm.build_market_breakdown(
        db_path=db, entropy_path=str(tmp_path / "nonexistent.json")
    )
    assert result["summary"]["entropy_snapshot_stale"] is True
    assert result["summary"]["entropy_load_error"] is not None


def test_stale_true_when_snapshot_old(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ENTROPY_STALE_GRACE_SEC", "30")
    importlib.reload(mbm)
    ep = _write_entropy_fixture(tmp_path, total=5, z_ready=2, age_sec=60)
    db = _make_minimal_db(tmp_path)
    result = mbm.build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_snapshot_stale"] is True
    monkeypatch.delenv("ENTROPY_STALE_GRACE_SEC", raising=False)
    importlib.reload(mbm)


def test_summary_contains_total_windows_and_z_ready(tmp_path: Path) -> None:
    ep = _write_entropy_fixture(tmp_path, total=7, z_ready=3, age_sec=5)
    db = _make_minimal_db(tmp_path)
    result = mbm.build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_total_windows"] == 7
    assert result["summary"]["entropy_z_ready_count"] == 3
