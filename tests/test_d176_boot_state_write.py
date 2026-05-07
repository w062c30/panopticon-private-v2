import json
import pytest
from pathlib import Path
from unittest.mock import patch

import panopticon_py.hunting.run_radar as radar_mod


def _make_boot_state():
    from panopticon_py.hunting.run_radar import RadarBootState, RadarState

    return RadarBootState(boot_id="test-001", state=RadarState.STARTING)


def test_dump_state_success(tmp_path: Path, monkeypatch):
    """Normal write should succeed without error."""
    monkeypatch.setattr(radar_mod, "_RADAR_BOOT_STATE_PATH", tmp_path / "radar_boot_state.json")
    monkeypatch.setattr(radar_mod, "_radar_boot_state", _make_boot_state())
    radar_mod._dump_radar_boot_state()
    data = json.loads((tmp_path / "radar_boot_state.json").read_text())
    assert data["state"] == "starting"
    assert data["boot_id"] == "test-001"


def test_dump_state_permission_error_retries_then_direct(tmp_path: Path, monkeypatch):
    """tmp.replace() PermissionError should fallback to direct write without raising."""
    target = tmp_path / "radar_boot_state.json"
    monkeypatch.setattr(radar_mod, "_RADAR_BOOT_STATE_PATH", target)
    monkeypatch.setattr(radar_mod, "_radar_boot_state", _make_boot_state())

    call_count = {"n": 0}
    original_replace = Path.replace

    def flaky_replace(self, target_path):
        call_count["n"] += 1
        if call_count["n"] <= radar_mod._DUMP_STATE_MAX_RETRIES:
            raise PermissionError("WinError 5")
        return original_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", flaky_replace)
    radar_mod._dump_radar_boot_state()
    assert target.exists()
    data = json.loads(target.read_text())
    assert data["state"] == "starting"


def test_dump_state_all_paths_fail_no_raise(tmp_path: Path, monkeypatch):
    """Even when all write paths fail, the function must not raise (hot path protection)."""
    target = tmp_path / "radar_boot_state.json"
    monkeypatch.setattr(radar_mod, "_RADAR_BOOT_STATE_PATH", target)
    monkeypatch.setattr(radar_mod, "_radar_boot_state", _make_boot_state())

    def always_fail(self, *a, **kw):
        raise PermissionError("always fail")

    monkeypatch.setattr(Path, "replace", always_fail)
    monkeypatch.setattr(Path, "write_text", always_fail)
    radar_mod._dump_radar_boot_state()


def test_manifest_status_update(tmp_path: Path, monkeypatch):
    """_update_radar_manifest_status should update status field."""
    manifest = tmp_path / "process_manifest.json"
    manifest.write_text(json.dumps({"version": "v1.3.3", "status": "initializing"}))
    monkeypatch.setattr(radar_mod, "_RADAR_MANIFEST_PATH", manifest)
    radar_mod._update_radar_manifest_status("ready")
    data = json.loads(manifest.read_text())
    assert data["status"] == "ready"
    assert "status_updated_at" in data


def test_manifest_status_no_file_is_noop(tmp_path: Path, monkeypatch):
    """When manifest does not exist, should silently return."""
    monkeypatch.setattr(radar_mod, "_RADAR_MANIFEST_PATH", tmp_path / "nonexistent.json")
    radar_mod._update_radar_manifest_status("ready")


def test_set_radar_state_triggers_manifest_update(tmp_path: Path, monkeypatch):
    """_set_radar_state(READY) should call _update_radar_manifest_status."""
    manifest = tmp_path / "process_manifest.json"
    manifest.write_text(json.dumps({"version": "v1.3.3", "status": "initializing"}))
    monkeypatch.setattr(radar_mod, "_RADAR_MANIFEST_PATH", manifest)
    monkeypatch.setattr(radar_mod, "_RADAR_BOOT_STATE_PATH", tmp_path / "radar_boot_state.json")
    from panopticon_py.hunting.run_radar import RadarBootState, RadarState

    monkeypatch.setattr(radar_mod, "_radar_boot_state", RadarBootState(boot_id="test-002", state=RadarState.CONNECTING))
    radar_mod._set_radar_state(RadarState.READY, force=True)
    data = json.loads(manifest.read_text())
    assert data["status"] == "ready"