"""D193 — entropy pipeline health diagnostics: global declaration, quiet-market log, z_threshold."""

import inspect
import time


def test_d75_hb_real_trade_base_module_level():
    """RULE-CLOSURE-1: _d75_hb_real_trade_base must be declared at module level."""
    import panopticon_py.hunting.run_radar as rr
    assert hasattr(rr, "_d75_hb_real_trade_base"), (
        "D193: _d75_hb_real_trade_base must be declared at module level — RULE-CLOSURE-1"
    )
    assert isinstance(rr._d75_hb_real_trade_base, int), (
        "_d75_hb_real_trade_base must be int, got %s" % type(rr._d75_hb_real_trade_base)
    )


def test_d193_quiet_market_counter_module_level():
    """D193: _D193_QUIET_MARKET_COUNTER must be at module level for RULE-CLOSURE-1."""
    import panopticon_py.hunting.run_radar as rr
    assert hasattr(rr, "_D193_QUIET_MARKET_COUNTER"), (
        "D193: _D193_QUIET_MARKET_COUNTER must be declared at module level"
    )
    assert isinstance(rr._D193_QUIET_MARKET_COUNTER, int), (
        "_D193_QUIET_MARKET_COUNTER must be int, got %s" % type(rr._D193_QUIET_MARKET_COUNTER)
    )


def test_live_ticks_unlocked_global_declares_real_trade_base():
    """RULE-CLOSURE-1: _live_ticks_unlocked must declare global _d75_hb_real_trade_base."""
    import panopticon_py.hunting.run_radar as rr
    src = inspect.getsource(rr._live_ticks_unlocked)
    assert "global _d75_hb_real_trade_base" in src, (
        "D193: _live_ticks_unlocked must declare 'global _d75_hb_real_trade_base'"
    )


def test_live_ticks_unlocked_global_declares_quiet_market_counter():
    """RULE-CLOSURE-1: _live_ticks_unlocked must declare global _D193_QUIET_MARKET_COUNTER."""
    import panopticon_py.hunting.run_radar as rr
    src = inspect.getsource(rr._live_ticks_unlocked)
    assert "global _D193_QUIET_MARKET_COUNTER" in src, (
        "D193: _live_ticks_unlocked must declare 'global _D193_QUIET_MARKET_COUNTER'"
    )


def test_metrics_json_loop_global_declares_quiet_market_counter():
    """RULE-CLOSURE-1: _metrics_json_loop must declare global _D193_QUIET_MARKET_COUNTER."""
    import panopticon_py.hunting.run_radar as rr
    src = inspect.getsource(rr._metrics_json_loop)
    assert "global _D193_QUIET_MARKET_COUNTER" in src, (
        "D193: _metrics_json_loop must declare 'global _D193_QUIET_MARKET_COUNTER'"
    )


def test_z_threshold_is_negative():
    """z_threshold must be negative for negative-entropy detection (fire when z < threshold)."""
    from config import get_z_threshold
    z = get_z_threshold()
    assert z < 0.0, (
        f"z_threshold={z} must be negative (fire when z-score < threshold for negative entropy). "
        f"Got positive value — check HUNT_MIN_ENTROPY_Z_THRESHOLD config."
    )
    # Sanity: extremely high threshold (less negative) means fewer fires, lower means more fires
    assert z >= -10.0, (
        f"z_threshold={z} is too negative — will fire on every slight dip. "
        f"Consider HUNT_MIN_ENTROPY_Z_THRESHOLD between -5.0 and -1.5."
    )


def test_entropy_window_should_fire_negative_entropy_returns_bool():
    """
    D193: Verify should_fire_negative_entropy returns a bool and fires correctly.

    Logic: returns z < z_threshold (negative entropy when z is below threshold).
    - threshold=100.0 (very positive): z ≈ 0.0 is < 100.0 → fires (positive entropy, z near 0 < high threshold)
    - threshold=-999.0 (very negative): z ≈ 0.0 is NOT < -999.0 → does not fire (z is above the threshold)
    """
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow(tier="t3", window_sec=60.0, min_history_for_z=3)
    base = time.monotonic()

    # Warm the window with enough samples for z-score calculation
    for i in range(10):
        ew.push(recv_mono=base + i * 5.0, buy_vol=100.0, sell_vol=50.0)
    for _ in range(5):
        ew.record_H_sample(recv_mono=base + 45.0)

    # With very high positive threshold, normal z-score (~0) should fire (z < 100.0)
    result_high = ew.should_fire_negative_entropy(100.0)
    assert result_high is True, (
        f"With z_threshold=100.0, normal z-score should fire. Got {result_high}"
    )

    # With very negative threshold, z-score (~0) should NOT fire (z not < -999.0)
    result_neg = ew.should_fire_negative_entropy(-999.0)
    assert result_neg is False, (
        f"With z_threshold=-999.0, z-score ({ew.zscore_of_latest_delta()[1]}) should NOT fire. "
        f"Got {result_neg}"
    )


def test_process_version_bumped_for_d193():
    """PROCESS_VERSION in run_radar.py must reflect D193."""
    import panopticon_py.hunting.run_radar as rr
    assert "D193" in rr.PROCESS_VERSION, (
        "D193: run_radar.py PROCESS_VERSION must contain 'D193', got: " + rr.PROCESS_VERSION
    )


def test_orchestrator_version_bumped_for_d193():
    """PROCESS_VERSION in run_hft_orchestrator.py must reflect D193 (paired)."""
    import run_hft_orchestrator as orch
    assert "D193" in orch.PROCESS_VERSION, (
        "D193: run_hft_orchestrator.py PROCESS_VERSION must contain 'D193', got: " + orch.PROCESS_VERSION
    )