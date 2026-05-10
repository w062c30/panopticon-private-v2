"""D191 — recon DB lock skip telemetry for RVF pipeline metrics."""

from panopticon_py.metrics import MetricsCollector
from panopticon_py.metrics.metrics_schema import PipelineStats


def test_on_recon_lock_skip_increments_counter() -> None:
    mc = MetricsCollector()
    assert mc._recon_lock_skip_rc.count() == 0
    mc.on_recon_lock_skip()
    assert mc._recon_lock_skip_rc.count() == 1


def test_collect_exposes_recon_lock_skip_60s() -> None:
    mc = MetricsCollector()
    mc.on_recon_lock_skip()
    mc.on_recon_lock_skip()
    mc.on_recon_lock_skip()
    snap = mc.collect()
    assert snap.pipeline.recon_lock_skip_60s == 3


def test_pipeline_stats_to_dict_includes_recon_lock_skip_60s() -> None:
    payload = PipelineStats(recon_lock_skip_60s=7).to_dict()
    assert payload["recon_lock_skip_60s"] == 7
