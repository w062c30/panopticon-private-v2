"""D180: PipelineStats derived ratios from MetricsCollector + entropy_status.json."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from panopticon_py.metrics.metrics_collector import MetricsCollector


def test_pipeline_ratios_from_entropy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    es_path = data_dir / "entropy_status.json"
    es_path.write_text(
        json.dumps(
            {
                "total": 10,
                "z_ready_count": 4,
                "locked_count": 2,
                "tokens": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENTROPY_STATUS_PATH", str(es_path))

    mc = MetricsCollector()
    mc.on_ws_connected()
    mc.on_trade_tick()
    mc.on_trade_tick()
    mc.on_gate_result(True)
    mc.on_gate_result(False)
    mc.on_signal_processed()
    mc.on_kyle_compute("0xabc", 0.01)

    snap = mc.collect()
    assert snap.pipeline.z_ready_ratio == pytest.approx(0.4)
    assert snap.pipeline.l0_locked_ratio == pytest.approx(0.2)
    assert snap.pipeline.entropy_warmup_ratio == pytest.approx(0.4)
    assert snap.pipeline.active_window_breakdown["total"] == 10
    assert snap.pipeline.active_window_breakdown["ready"] == 4
    assert snap.pipeline.active_window_breakdown["locked"] == 2
    assert snap.pipeline.active_window_breakdown["warming"] == 4

    assert snap.pipeline.gate_pass_rate_60s == pytest.approx(0.5)
    assert snap.pipeline.fire_rate_60s == pytest.approx(0.0)
    assert snap.pipeline.input_to_processed_ratio_60s == pytest.approx(0.5)
    assert snap.to_dict()["pipeline"]["z_ready_ratio"] == pytest.approx(0.4)


def test_pipeline_gate_eval_zero_uses_denominator_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    es_path = tmp_path / "entropy_status.json"
    es_path.write_text(json.dumps({"total": 0, "z_ready_count": 0, "locked_count": 0}))
    monkeypatch.setenv("ENTROPY_STATUS_PATH", str(es_path))

    mc = MetricsCollector()
    mc.on_entropy_fire(-5.0)
    snap = mc.collect()
    assert snap.pipeline.fire_rate_60s == pytest.approx(1.0)
    assert snap.pipeline.gate_pass_rate_60s == pytest.approx(0.0)
