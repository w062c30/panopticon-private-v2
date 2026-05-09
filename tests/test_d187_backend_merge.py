"""D187: Backend merges orchestrator sidecar into RVF snapshot."""

import json

import pytest


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    """Isolate snapshot paths so imports do not read real data/."""
    monkeypatch.setenv("RVF_SNAPSHOT_PATH", str(tmp_path / "radar.json"))
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(tmp_path / "orch.json"))
    from panopticon_py.api import app as app_mod

    return app_mod


def test_read_rvf_merges_orchestrator_metrics(app_module, tmp_path):
    radar = tmp_path / "radar.json"
    radar.write_text(
        json.dumps(
            {
                "ts_utc": "2026-05-10T00:00:00+00:00",
                "pipeline": {"z_ready_ratio": 0.5, "l2_eval_60s": 0},
                "gate": {"evaluated_60s": 0},
            }
        ),
        encoding="utf-8",
    )
    orch = tmp_path / "orch.json"
    orch.write_text(
        json.dumps(
            {
                "ts_utc": "2026-05-10T00:00:01+00:00",
                "pipeline": {"l2_eval_60s": 7, "l3_eval_60s": 2, "z_ready_ratio": 0.99},
                "gate": {
                    "evaluated_60s": 2,
                    "pass_count_60s": 0,
                    "abort_count_60s": 2,
                },
                "_written_at": 1715123456.78,
            }
        ),
        encoding="utf-8",
    )
    out = app_module._read_rvf_snapshot()
    assert "error" not in out
    assert out["pipeline"]["z_ready_ratio"] == 0.5
    assert out["pipeline"]["l2_eval_60s"] == 0
    om = out["orchestrator_metrics"]
    assert om["pipeline"]["l2_eval_60s"] == 7
    assert om["pipeline"]["l3_eval_60s"] == 2
    assert om["gate"]["evaluated_60s"] == 2
    assert om["gate"]["pass_count_60s"] == 0
    assert om["gate"]["abort_count_60s"] == 2
    assert om["_written_at"] == 1715123456.78
    assert "stale_seconds" in om
    assert om["stale_seconds"] is not None
    assert om["stale_seconds"] >= 0


def test_read_rvf_missing_sidecar_no_block(app_module, tmp_path):
    radar = tmp_path / "radar.json"
    radar.write_text(json.dumps({"ok": True}), encoding="utf-8")
    out = app_module._read_rvf_snapshot()
    assert out.get("ok") is True
    assert "orchestrator_metrics" not in out


def test_read_rvf_invalid_sidecar_ignored(app_module, tmp_path):
    radar = tmp_path / "radar.json"
    radar.write_text(json.dumps({"x": 1}), encoding="utf-8")
    orch = tmp_path / "orch.json"
    orch.write_text("not-json", encoding="utf-8")
    out = app_module._read_rvf_snapshot()
    assert out.get("x") == 1
    assert "orchestrator_metrics" not in out
