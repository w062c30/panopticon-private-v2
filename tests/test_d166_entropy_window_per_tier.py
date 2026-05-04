"""D166: tests for tier-aware entropy window behavior."""

from __future__ import annotations

import pytest


def _clear_entropy_window_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUNT_ENTROPY_WINDOW_SEC", raising=False)
    monkeypatch.delenv("HUNT_ENTROPY_WINDOW_SEC_T1", raising=False)
    monkeypatch.delenv("HUNT_ENTROPY_WINDOW_SEC_T2", raising=False)
    monkeypatch.delenv("HUNT_ENTROPY_WINDOW_SEC_T3", raising=False)
    monkeypatch.delenv("HUNT_ENTROPY_WINDOW_SEC_T5", raising=False)


def test_tier_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_entropy_window_env(monkeypatch)
    from panopticon_py.hunting.entropy_window import EntropyWindow

    assert EntropyWindow(tier="t1").window_sec == 5.0
    assert EntropyWindow(tier="t2").window_sec == 60.0
    assert EntropyWindow(tier="t3").window_sec == 60.0
    assert EntropyWindow(tier="t5").window_sec == 60.0


def test_t2_low_frequency_can_accumulate_history(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_entropy_window_env(monkeypatch)
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow(tier="t2")
    t0 = 1000.0
    for i in range(4):
        ew.push(t0 + i * 30.0, 1.0, 0.0)
        ew.record_H_sample(t0 + i * 30.0)

    assert len(ew._events) == 3
    assert len(ew._h_history) >= 1


def test_per_tier_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_entropy_window_env(monkeypatch)
    monkeypatch.setenv("HUNT_ENTROPY_WINDOW_SEC_T2", "10")
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow(tier="t2")
    assert ew.window_sec == 10.0


def test_legacy_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_entropy_window_env(monkeypatch)
    monkeypatch.setenv("HUNT_ENTROPY_WINDOW_SEC", "20")
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow(tier="t2")
    assert ew.window_sec == 20.0


def test_get_or_create_ew_tier_migration(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_entropy_window_env(monkeypatch)
    from panopticon_py.hunting import run_radar

    run_radar._entropy_windows.clear()
    run_radar._token_tier_map.clear()

    ew = run_radar._get_or_create_ew("token-test", tier="t1")
    assert ew.tier == "t1"
    assert ew.window_sec == 5.0

    migrated = run_radar._get_or_create_ew("token-test", tier="t2")
    assert migrated is ew
    assert migrated.tier == "t2"
    assert migrated.window_sec == 60.0
    assert migrated.state_dict()["trigger_locked"] is True
