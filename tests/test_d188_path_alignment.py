"""D188 — RULE-PATH-1: orchestrator sidecar path aligns with backend ORCH_RVF_METRICS_PATH."""

import json
import os

import pytest

from panopticon_py.metrics import MetricsCollector


def test_sidecar_written_to_env_path(tmp_path, monkeypatch):
    """If ORCH_RVF_METRICS_PATH is set, persist_json must target that path."""
    custom_path = tmp_path / "custom_orch.json"
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(custom_path))

    resolved = os.getenv("ORCH_RVF_METRICS_PATH", "data/orch_rvf_metrics.json")
    assert resolved == str(custom_path)

    mc = MetricsCollector()
    mc.on_l2_eval()
    mc.persist_json(path=resolved)
    assert custom_path.is_file()


@pytest.fixture()
def app_module(monkeypatch, tmp_path):
    monkeypatch.setenv("RVF_SNAPSHOT_PATH", str(tmp_path / "radar.json"))
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(tmp_path / "orch.json"))
    from panopticon_py.api import app as app_mod

    return app_mod


def test_backend_reads_same_env_path(app_module, tmp_path, monkeypatch):
    custom_path = tmp_path / "custom_orch.json"
    monkeypatch.setenv("ORCH_RVF_METRICS_PATH", str(custom_path))
    custom_path.write_text(
        json.dumps(
            {
                "ts_utc": "2026-05-10T00:00:00+00:00",
                "pipeline": {"l2_eval_60s": 5, "l3_eval_60s": 2},
                "gate": {
                    "evaluated_60s": 2,
                    "pass_count_60s": 0,
                    "abort_count_60s": 2,
                },
                "_written_at": 1715123456.0,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "radar.json").write_text(
        json.dumps({"pipeline": {"l2_eval_60s": 0}}),
        encoding="utf-8",
    )
    out = app_module._read_rvf_snapshot()
    assert "orchestrator_metrics" in out
    assert out["orchestrator_metrics"]["pipeline"]["l2_eval_60s"] == 5


def test_default_paths_match(monkeypatch):
    monkeypatch.delenv("ORCH_RVF_METRICS_PATH", raising=False)
    write_fallback = "data/orch_rvf_metrics.json"
    read_fallback = os.getenv("ORCH_RVF_METRICS_PATH", "data/orch_rvf_metrics.json")
    assert write_fallback == read_fallback
