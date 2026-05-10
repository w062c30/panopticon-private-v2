"""D190 — reboot root cause: non-blocking order recon + boot deadline reset wiring."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from panopticon_py.metrics import MetricsCollector


def test_try_match_or_open_returns_empty_on_db_lock_without_sleep(monkeypatch):
    """D190: must not call time.sleep when DB reports locked (asyncio event loop safety)."""
    sleeps: list[float] = []

    def _track_sleep(sec: float) -> None:
        sleeps.append(sec)

    monkeypatch.setattr("panopticon_py.hunting.run_radar.time.sleep", _track_sleep)

    def _impl_raises(_raw, _db):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(
        "panopticon_py.ingestion.order_reconstruction_engine.try_match_or_open",
        _impl_raises,
    )

    from panopticon_py.hunting import run_radar as rr

    out = rr.try_match_or_open({}, MagicMock())
    assert out == ""
    assert sleeps == []


def test_try_match_or_open_propagates_non_lock_sqlite_errors(monkeypatch):
    def _impl_raises(_raw, _db):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(
        "panopticon_py.ingestion.order_reconstruction_engine.try_match_or_open",
        _impl_raises,
    )

    from panopticon_py.hunting import run_radar as rr

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        rr.try_match_or_open({}, MagicMock())


def test_persist_json_merges_process_start_ts(tmp_path):
    mc = MetricsCollector()
    path = tmp_path / "orch.json"
    ts = 1_700_000_000.0
    mc.persist_json(path=str(path), extra={"process_start_ts": ts})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["process_start_ts"] == ts
    assert "_written_at" in data


def test_live_ticks_ws_connect_resets_deadline_in_source():
    """Regression anchor: _on_ws_connected must refresh boot payload deadline (D190)."""
    src = Path("panopticon_py/hunting/run_radar.py").read_text(encoding="utf-8")
    assert "def _on_ws_connected() -> None:" in src
    assert "nonlocal _first_payload_deadline" in src
    assert "_first_payload_deadline = time.monotonic() + _BOOT_TIMEOUT_SEC" in src


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RVF_SNAPSHOT_PATH", str(tmp_path / "radar.json"))
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(tmp_path / "orch.json"))
    from panopticon_py.api import app as app_mod

    return app_mod


def test_read_rvf_merges_process_start_ts(app_module, tmp_path):
    radar = tmp_path / "radar.json"
    radar.write_text(json.dumps({"ts_utc": "2026-05-10T00:00:00+00:00"}), encoding="utf-8")
    orch = tmp_path / "orch.json"
    orch.write_text(
        json.dumps(
            {
                "ts_utc": "2026-05-10T00:00:01+00:00",
                "pipeline": {"l2_eval_60s": 1, "l3_eval_60s": 0},
                "gate": {"evaluated_60s": 0, "pass_count_60s": 0, "abort_count_60s": 0},
                "_written_at": 1_700_000_001.0,
                "process_start_ts": 1_700_000_000.0,
            }
        ),
        encoding="utf-8",
    )
    out = app_module._read_rvf_snapshot()
    om = out["orchestrator_metrics"]
    assert om["process_start_ts"] == 1_700_000_000.0
