"""
D184a: entropy key collision policy — setdefault (first-write wins) + counter.
Covers: same bare token with and without 0x prefix coexisting in snapshot.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


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


def _write_entropy(tmp_path: Path, tokens: dict[str, dict]) -> str:
    p = tmp_path / "entropy_status.json"
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
    payload = {
        "updated_ts": ts,
        "total": len(tokens),
        "z_ready_count": sum(1 for v in tokens.values() if v.get("z_ready")),
        "locked_count": 0,
        "tokens": tokens,
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _insert_hit(db_path: str, *market_ids: str) -> None:
    conn = sqlite3.connect(db_path)
    for mid in market_ids:
        conn.execute(
            "INSERT INTO hunting_shadow_hits VALUES (?, 2.5, 0.1, '2026-05-09T00:00:00Z')",
            (mid,),
        )
    conn.commit()
    conn.close()


def test_no_collision_normal_case(tmp_path: Path) -> None:
    tokens = {
        "0xAAA111": {
            "h_hist": 5,
            "need": 5,
            "trigger_locked": False,
            "events": 10,
            "healthy_span": 100.0,
            "z_ready": True,
        },
        "0xBBB222": {
            "h_hist": 3,
            "need": 5,
            "trigger_locked": False,
            "events": 6,
            "healthy_span": 60.0,
            "z_ready": False,
        },
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy(tmp_path, tokens)
    _insert_hit(db, "0xAAA111", "0xBBB222")

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_key_collisions"] == 0


def test_collision_detected_when_0x_and_bare_coexist(tmp_path: Path) -> None:
    tokens = {
        "0xABC123": {
            "h_hist": 10,
            "need": 5,
            "trigger_locked": False,
            "events": 20,
            "healthy_span": 300.0,
            "z_ready": True,
        },
        "abc123": {
            "h_hist": 3,
            "need": 5,
            "trigger_locked": True,
            "events": 6,
            "healthy_span": 50.0,
            "z_ready": False,
        },
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy(tmp_path, tokens)
    _insert_hit(db, "0xABC123", "abc123")

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_key_collisions"] >= 1


def test_first_write_wins_on_collision(tmp_path: Path) -> None:
    tokens = {
        "0xABC123": {
            "h_hist": 10,
            "need": 5,
            "trigger_locked": False,
            "events": 20,
            "healthy_span": 300.0,
            "z_ready": True,
        },
        "abc123": {
            "h_hist": 3,
            "need": 5,
            "trigger_locked": True,
            "events": 6,
            "healthy_span": 50.0,
            "z_ready": False,
        },
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy(tmp_path, tokens)
    _insert_hit(db, "0xABC123")

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    rows = {r["market_id"]: r for r in result["rows"]}
    assert "0xABC123" in rows
    assert rows["0xABC123"]["h_hist"] == 10


def test_no_collision_when_bare_only(tmp_path: Path) -> None:
    tokens = {
        "plaintoken": {
            "h_hist": 5,
            "need": 5,
            "trigger_locked": False,
            "events": 10,
            "healthy_span": 100.0,
            "z_ready": True,
        }
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy(tmp_path, tokens)
    _insert_hit(db, "plaintoken")

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_key_collisions"] == 0


def test_collision_count_accurate_with_multiple_pairs(tmp_path: Path) -> None:
    tokens = {
        "0xAAA": {
            "h_hist": 5,
            "need": 5,
            "trigger_locked": False,
            "events": 10,
            "healthy_span": 100.0,
            "z_ready": True,
        },
        "aaa": {
            "h_hist": 2,
            "need": 5,
            "trigger_locked": False,
            "events": 4,
            "healthy_span": 40.0,
            "z_ready": False,
        },
        "0xBBB": {
            "h_hist": 7,
            "need": 5,
            "trigger_locked": False,
            "events": 14,
            "healthy_span": 200.0,
            "z_ready": True,
        },
        "bbb": {
            "h_hist": 1,
            "need": 5,
            "trigger_locked": False,
            "events": 2,
            "healthy_span": 20.0,
            "z_ready": False,
        },
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy(tmp_path, tokens)

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    assert result["summary"]["entropy_key_collisions"] == 2
