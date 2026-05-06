import json
from pathlib import Path

import pytest

from panopticon_py.hunting import run_radar


def test_boot_id_is_unique():
    a = run_radar._new_boot_id()
    b = run_radar._new_boot_id()
    assert a != b
    assert a.startswith("radar-")
    assert b.startswith("radar-")


def test_invalid_transition_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "radar_boot_state.json"
    monkeypatch.setattr(run_radar, "_RADAR_BOOT_STATE_PATH", out)
    run_radar._radar_boot_state = run_radar.RadarBootState(
        boot_id="test",
        state=run_radar.RadarState.FAILED,
    )
    run_radar._set_radar_state(run_radar.RadarState.READY)
    assert run_radar._radar_boot_state is not None
    assert run_radar._radar_boot_state.state == run_radar.RadarState.FAILED


def test_mark_boot_failure_sets_failed_and_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "radar_boot_state.json"
    monkeypatch.setattr(run_radar, "_RADAR_BOOT_STATE_PATH", out)
    run_radar._radar_boot_state = run_radar.RadarBootState(
        boot_id="test-fail",
        state=run_radar.RadarState.SYNCING,
    )
    run_radar.mark_radar_boot_failure("boom")
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["state"] == "failed"
    assert payload["last_error"] == "boom"

