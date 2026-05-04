"""D165: tests for entropy_window unlock threshold env overrides."""

from __future__ import annotations

import pytest


def test_unlock_event_count_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_EW_UNLOCK_EVENT_COUNT", "3")
    from panopticon_py.hunting.entropy_window import EntropyWindow

    with pytest.raises(ValueError, match="too low"):
        EntropyWindow()


def test_unlock_event_count_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_EW_UNLOCK_EVENT_COUNT", "10")
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow()
    assert ew._unlock_event_count == 10


def test_unlock_healthy_span_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_EW_UNLOCK_HEALTHY_SPAN_SEC", "0.5")
    from panopticon_py.hunting.entropy_window import EntropyWindow

    with pytest.raises(ValueError, match="too low"):
        EntropyWindow()


def test_unlock_event_count_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUNT_EW_UNLOCK_EVENT_COUNT", raising=False)
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow()
    assert ew._unlock_event_count == 30


def test_trigger_locked_unlocks_with_fewer_events(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_EW_UNLOCK_EVENT_COUNT", "5")  # minimum = 5
    from panopticon_py.hunting.entropy_window import EntropyWindow

    ew = EntropyWindow()
    assert ew._trigger_locked is False
    ew._trigger_locked = True  # simulate locked after reconnect

    t = 1000.0
    for i in range(5):
        ew.push(t + i * 0.1, 1.0, 0.0)

    # With HUNT_EW_UNLOCK_EVENT_COUNT=5, after 5 events the lock should clear
    assert ew._trigger_locked is False
