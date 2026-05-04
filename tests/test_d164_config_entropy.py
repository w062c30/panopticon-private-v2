"""D164: config.get_z_threshold / get_min_history_for_z env overrides."""

from __future__ import annotations

import pytest


def test_get_z_threshold_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_MIN_ENTROPY_Z_THRESHOLD", "-3.25")
    from config import get_z_threshold

    assert get_z_threshold() == -3.25


def test_get_z_threshold_positive_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_MIN_ENTROPY_Z_THRESHOLD", "0.5")
    from config import get_z_threshold

    with pytest.raises(ValueError, match="must be negative"):
        get_z_threshold()


def test_get_min_history_too_low_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HUNT_MIN_HISTORY_FOR_Z", "2")
    from config import get_min_history_for_z

    with pytest.raises(ValueError, match="min=3"):
        get_min_history_for_z()


def test_get_min_history_default_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HUNT_MIN_HISTORY_FOR_Z", raising=False)
    from config import get_min_history_for_z

    assert get_min_history_for_z() == 5
