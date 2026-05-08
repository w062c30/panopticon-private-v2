"""
D183 Q1 Option B: removeprefix('0x') correctness regression.
Verifies non-hex-prefixed token IDs are not mutated and 0x-prefixed tokens
are still correctly resolved.
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


def _write_entropy_with_tokens(tmp_path: Path, tokens: dict[str, dict]) -> str:
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


def _insert_hit(db_path: str, market_id: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO hunting_shadow_hits VALUES (?, 2.5, 0.1, '2026-05-09T00:00:00Z')",
        (market_id,),
    )
    conn.commit()
    conn.close()


def test_0x_prefix_token_lookup_still_works(tmp_path: Path) -> None:
    token_id = "0xABCDEF1234567890"
    tokens = {
        token_id: {
            "h_hist": 10,
            "need": 5,
            "trigger_locked": False,
            "events": 20,
            "healthy_span": 300.0,
            "z_ready": True,
        }
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy_with_tokens(tmp_path, tokens)
    _insert_hit(db, token_id)

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    matched = [r for r in result["rows"] if r["market_id"] == token_id]
    assert len(matched) == 1
    assert matched[0]["h_hist"] == 10
    assert matched[0]["z_ready"] is True


def test_x_leading_token_not_mutated(tmp_path: Path) -> None:
    token_id = "xyztoken_not_hex_prefixed"
    tokens = {
        token_id: {
            "h_hist": 7,
            "need": 5,
            "trigger_locked": False,
            "events": 14,
            "healthy_span": 200.0,
            "z_ready": True,
        }
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy_with_tokens(tmp_path, tokens)
    _insert_hit(db, token_id)

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    matched = [r for r in result["rows"] if r["market_id"] == token_id]
    assert len(matched) == 1
    assert matched[0]["h_hist"] == 7


def test_zero_zero_x_prefix_not_over_stripped(tmp_path: Path) -> None:
    token_id = "00xabc_double_zero"
    tokens = {
        token_id: {
            "h_hist": 3,
            "need": 5,
            "trigger_locked": True,
            "events": 6,
            "healthy_span": 50.0,
            "z_ready": False,
        }
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy_with_tokens(tmp_path, tokens)
    _insert_hit(db, token_id)

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    matched = [r for r in result["rows"] if r["market_id"] == token_id]
    assert len(matched) == 1
    assert matched[0]["locked"] is True


def test_uppercase_0x_token_normalized(tmp_path: Path) -> None:
    token_id = "0XABCDEF"
    tokens = {
        token_id: {
            "h_hist": 8,
            "need": 5,
            "trigger_locked": False,
            "events": 16,
            "healthy_span": 400.0,
            "z_ready": True,
        }
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy_with_tokens(tmp_path, tokens)
    _insert_hit(db, token_id)

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    matched = [r for r in result["rows"] if r["market_id"] == token_id]
    assert len(matched) == 1
    assert matched[0]["z_ready"] is True


def test_mixed_prefix_and_no_prefix_tokens_coexist(tmp_path: Path) -> None:
    tokens = {
        "0xAAA111": {
            "h_hist": 5,
            "need": 5,
            "trigger_locked": False,
            "events": 10,
            "healthy_span": 100.0,
            "z_ready": True,
        },
        "plaintoken999": {
            "h_hist": 3,
            "need": 5,
            "trigger_locked": False,
            "events": 6,
            "healthy_span": 60.0,
            "z_ready": False,
        },
    }
    db = _make_minimal_db(tmp_path)
    ep = _write_entropy_with_tokens(tmp_path, tokens)
    _insert_hit(db, "0xAAA111")
    _insert_hit(db, "plaintoken999")

    from panopticon_py.diagnostics.market_breakdown import build_market_breakdown

    result = build_market_breakdown(db_path=db, entropy_path=ep)
    by_id = {r["market_id"]: r for r in result["rows"]}
    assert "0xAAA111" in by_id
    assert "plaintoken999" in by_id
    assert by_id["0xAAA111"]["z_ready"] is True
    assert by_id["plaintoken999"]["z_ready"] is False
