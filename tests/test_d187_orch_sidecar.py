"""D187: Orchestrator-style MetricsCollector JSON sidecar (persist_json)."""

import json

from panopticon_py.metrics import MetricsCollector


def test_persist_json_includes_l2_l3_pipeline_counters(tmp_path):
    mc = MetricsCollector()
    mc.on_l2_eval()
    mc.on_l2_eval()
    mc.on_l3_eval()
    path = tmp_path / "orch_rvf_metrics.json"
    mc.persist_json(path=str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "pipeline" in data
    assert int(data["pipeline"].get("l2_eval_60s") or 0) >= 2
    assert int(data["pipeline"].get("l3_eval_60s") or 0) >= 1
    assert "gate" in data
    assert not (tmp_path / "orch_rvf_metrics.json.tmp").exists()
