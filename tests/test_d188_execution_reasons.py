"""D188 — /api/diagnostics/execution_reasons and orchestrator_metrics.stale_seconds."""

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")


def _utc_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture()
def fake_db(tmp_path):
    db = tmp_path / "panopticon.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE execution_records (
            execution_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            accepted INTEGER NOT NULL,
            reason TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'PAPER',
            source TEXT NOT NULL DEFAULT 'radar',
            gate_reason TEXT,
            latency_ms REAL NOT NULL,
            created_ts_utc TEXT NOT NULL
        )
        """
    )
    now = datetime.now(timezone.utc)
    rows = [
        ("e1", "d1", 0, "rejected", "NO_PRICE_DATA", 1.0, _utc_iso(now - timedelta(minutes=5))),
        ("e2", "d2", 0, "rejected", "NO_PRICE_DATA", 1.0, _utc_iso(now - timedelta(minutes=4))),
        ("e3", "d3", 0, "rejected", "L4_SUPPRESSED", 1.0, _utc_iso(now - timedelta(minutes=3))),
        ("e4", "d4", 0, "NON_POSITIVE_TIME_ADJUSTED_EV", None, 1.0, _utc_iso(now - timedelta(minutes=2))),
        ("e5", "d5", 0, "", None, 1.0, _utc_iso(now - timedelta(minutes=1))),
    ]
    conn.executemany(
        """
        INSERT INTO execution_records (
            execution_id, decision_id, accepted, reason, gate_reason, latency_ms, created_ts_utc
        ) VALUES (?,?,?,?,?,?,?)
        """,
        rows,
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RVF_SNAPSHOT_PATH", str(tmp_path / "radar.json"))
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(tmp_path / "orch.json"))
    (tmp_path / "radar.json").write_text("{}", encoding="utf-8")
    from panopticon_py.api import app as app_mod

    return app_mod


def test_execution_reasons_aggregation(tmp_path, monkeypatch, fake_db, app_module):
    monkeypatch.setenv("PANOPTICON_DB_PATH", str(fake_db))

    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    resp = client.get("/api/diagnostics/execution_reasons?window_hours=168")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    reasons = {r["reason"]: r["count"] for r in data["reasons"]}
    assert reasons.get("NO_PRICE_DATA") == 2
    assert reasons.get("L4_SUPPRESSED") == 1
    assert reasons.get("NON_POSITIVE_TIME_ADJUSTED_EV") == 1
    assert reasons.get("UNKNOWN") == 1


def test_execution_reasons_window_validation(tmp_path, monkeypatch, fake_db, app_module):
    monkeypatch.setenv("PANOPTICON_DB_PATH", str(fake_db))

    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    assert client.get("/api/diagnostics/execution_reasons?window_hours=0").status_code == 400
    assert client.get("/api/diagnostics/execution_reasons?window_hours=200").status_code == 400
    assert client.get("/api/diagnostics/execution_reasons?window_hours=24").status_code == 200


def test_stale_seconds_in_orchestrator_metrics(tmp_path, monkeypatch, app_module):
    written_at = time.time() - 10.0
    orch_path = tmp_path / "orch.json"
    orch_path.write_text(
        json.dumps(
            {
                "ts_utc": "2026-05-10T00:00:00+00:00",
                "pipeline": {"l2_eval_60s": 3, "l3_eval_60s": 1},
                "gate": {"evaluated_60s": 1, "pass_count_60s": 0, "abort_count_60s": 1},
                "_written_at": written_at,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(orch_path))

    out = app_module._read_rvf_snapshot()
    om = out.get("orchestrator_metrics", {})
    assert "stale_seconds" in om
    assert om["stale_seconds"] is not None
    assert 5.0 <= om["stale_seconds"] <= 25.0
