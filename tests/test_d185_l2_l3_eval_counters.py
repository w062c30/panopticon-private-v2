"""D185: verify RVF L2/L3 evaluation counters are exposed in pipeline stats."""
from __future__ import annotations

from panopticon_py.metrics.metrics_collector import MetricsCollector


def test_l2_l3_eval_counters_increment_and_export() -> None:
    mc = MetricsCollector()
    mc.on_l2_eval()
    mc.on_l2_eval()
    mc.on_l3_eval()

    snap = mc.collect()
    assert snap.pipeline.l2_eval_60s == 2
    assert snap.pipeline.l3_eval_60s == 1

    payload = snap.to_dict()
    assert payload["pipeline"]["l2_eval_60s"] == 2
    assert payload["pipeline"]["l3_eval_60s"] == 1
