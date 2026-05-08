"""D181: fire_rate window alignment + stale warning throttle."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from panopticon_py.metrics.metrics_collector import MetricsCollector


def _set_entropy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    es = tmp_path / "entropy_status.json"
    es.write_text(json.dumps({"total": 0, "z_ready_count": 0, "locked_count": 0}), encoding="utf-8")
    monkeypatch.setenv("ENTROPY_STATUS_PATH", str(es))


def test_fire_rate_uses_60s_counter_alignment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_entropy_file(tmp_path, monkeypatch)
    mc = MetricsCollector()
    # Simulate 3 entropy fires and 6 gate evaluations in same 60s window.
    for _ in range(3):
        mc.on_entropy_fire(-5.0)
    for _ in range(6):
        mc.on_gate_result(False)
    snap = mc.collect()
    assert snap.pipeline.fire_rate_60s == pytest.approx(0.5)


def test_stale_warning_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    _set_entropy_file(tmp_path, monkeypatch)
    monkeypatch.setenv("ARB_STALE_WARN_SEC", "1")
    monkeypatch.setenv("ARB_STALE_CRIT_SEC", "2")
    monkeypatch.setenv("PIPELINE_STALE_WARN_INTERVAL_SEC", "9999")

    mc = MetricsCollector()
    # Force stale by setting last ws msg far in past.
    mc._last_ws_msg_ts = time.time() - 10_000
    caplog.set_level("WARNING")
    mc.collect()
    mc.collect()
    stale_logs = [r for r in caplog.records if "[PIPELINE][STALE_" in r.message]
    assert len(stale_logs) == 1
