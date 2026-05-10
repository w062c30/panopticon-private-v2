"""D189: EntropyWindow reconnect lock + T2/T3 min_history_for_z (low tick-rate)."""

import time

import pytest

from panopticon_py.hunting.entropy_window import EntropyWindow


def test_t2_low_tickrate_eventually_scores(monkeypatch):
    """With EW_MIN_HISTORY_FOR_Z=3, five trade ticks can yield a z-score (t2/t3 path)."""
    monkeypatch.setenv("EW_MIN_HISTORY_FOR_Z", "3")
    ew = EntropyWindow(tier="t3")
    assert ew.min_history_for_z == 3
    base = time.monotonic()
    for i in range(5):
        ew.push(base + float(i) * 10.0, 100.0, 0.0)
        ew.record_H_sample(base + float(i) * 10.0)
    _d, z = ew.zscore_of_latest_delta()
    assert z is not None, "expected z-score after 5 H samples with min_history_for_z=3"


def test_reconnect_does_not_perma_lock_when_h_history(monkeypatch):
    """D189: mark_reconnect must not lock tokens that already have H baseline."""
    monkeypatch.setenv("EW_MIN_HISTORY_FOR_Z", "3")
    ew = EntropyWindow(tier="t3")
    base = time.monotonic()
    for i in range(5):
        ew.push(base + float(i) * 10.0, 100.0, 0.0)
        ew.record_H_sample(base + float(i) * 10.0)
    assert len(ew._h_history) >= 1

    ew.mark_reconnect(reason="ws_disconnect")
    assert not ew._trigger_locked, (
        f"mark_reconnect must not lock when h_history={len(ew._h_history)}"
    )


def test_reconnect_still_locks_fresh_token(monkeypatch):
    monkeypatch.setenv("EW_MIN_HISTORY_FOR_Z", "3")
    ew = EntropyWindow(tier="t3")
    ew.push(time.monotonic(), 1.0, 0.0)
    ew.mark_reconnect(reason="ws_disconnect")
    assert ew._trigger_locked
    assert len(ew._h_history) == 0


def test_t1_uses_hunt_min_history_not_ew_env(monkeypatch):
    monkeypatch.setenv("EW_MIN_HISTORY_FOR_Z", "3")
    monkeypatch.setenv("HUNT_MIN_HISTORY_FOR_Z", "5")
    ew = EntropyWindow(tier="t1")
    assert ew.min_history_for_z == 5
