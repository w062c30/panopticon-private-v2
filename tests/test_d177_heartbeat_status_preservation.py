"""
D177: update_heartbeat() status preservation tests.
Tests that heartbeat loop does not overwrite semantic status values
("ready", "degraded", "failed") written by the process itself.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from unittest.mock import patch


class TestProcessOwnStatuses:
    def test_process_own_statuses_set_contents(self):
        """_PROCESS_OWN_STATUSES must contain exactly the expected values."""
        from panopticon_py.utils.process_guard import _PROCESS_OWN_STATUSES

        assert _PROCESS_OWN_STATUSES == frozenset({"ready", "degraded", "failed"})

    def test_process_own_statuses_is_frozenset(self):
        """_PROCESS_OWN_STATUSES must be immutable (frozenset)."""
        from panopticon_py.utils.process_guard import _PROCESS_OWN_STATUSES

        assert isinstance(_PROCESS_OWN_STATUSES, frozenset)

    def test_process_guard_version_bumped(self):
        """PROCESS_GUARD_VERSION must reflect D177 bump."""
        from panopticon_py.utils.process_guard import PROCESS_GUARD_VERSION

        assert "D177" in PROCESS_GUARD_VERSION


def _redirect_manifest(tmp_path: Path, data: dict):
    """Redirect _RUN_DIR, _MANIFEST, and _MANIFEST_LOCK to tmp_path."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "process_manifest.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    lock_path = run_dir / ".manifest.lock"

    patch_targets = [
        ("panopticon_py.utils.process_guard._RUN_DIR", run_dir),
        ("panopticon_py.utils.process_guard._MANIFEST", manifest_path),
        ("panopticon_py.utils.process_guard._MANIFEST_LOCK", lock_path),
    ]
    return patch_targets, manifest_path


class TestHeartbeatPreservesSemanticStatus:
    def test_heartbeat_preserves_ready_status(self, tmp_path: Path, monkeypatch):
        """update_heartbeat() must NOT overwrite 'ready' with 'running'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {
                "pid": 12345,
                "version": "v1.3.3-D176",
                "status": "ready",
                "status_updated_at": "2026-05-07T08:26:08.492Z",
            }
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")

        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "ready", (
            "update_heartbeat() must preserve 'ready' status"
        )
        assert "last_heartbeat_ts" in result["radar"]

    def test_heartbeat_preserves_degraded_status(self, tmp_path: Path, monkeypatch):
        """update_heartbeat() must NOT overwrite 'degraded'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {"pid": 1, "version": "v1.3.3", "status": "degraded"}
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "degraded"

    def test_heartbeat_preserves_failed_status(self, tmp_path: Path, monkeypatch):
        """update_heartbeat() must NOT overwrite 'failed'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {"pid": 1, "version": "v1.3.3", "status": "failed"}
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "failed"

    def test_heartbeat_still_writes_running_for_running(self, tmp_path: Path, monkeypatch):
        """update_heartbeat() SHOULD write 'running' when status is already 'running'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {"pid": 1, "version": "v1.3.3", "status": "running"}
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "running"

    def test_heartbeat_writes_running_for_initializing(self, tmp_path: Path, monkeypatch):
        """'initializing' is NOT in _PROCESS_OWN_STATUSES — heartbeat advances to 'running'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {"pid": 1, "version": "v1.3.3", "status": "initializing"}
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "running"

    def test_heartbeat_preserves_status_updated_at(self, tmp_path: Path, monkeypatch):
        """status_updated_at written by _update_radar_manifest_status must survive heartbeat."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {
                "pid": 1,
                "version": "v1.3.3",
                "status": "ready",
                "status_updated_at": "2026-05-07T08:26:08.492Z",
            }
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"].get("status_updated_at") == "2026-05-07T08:26:08.492Z"

    def test_heartbeat_preserves_other_fields(self, tmp_path: Path, monkeypatch):
        """All other manifest fields must survive heartbeat update."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {
                "pid": 99999,
                "version": "v1.3.3-D176",
                "status": "ready",
                "start_time": "2026-05-07T08:00:00.000Z",
                "host": "AMOY",
                "custom_field": "preserved",
            }
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["start_time"] == "2026-05-07T08:00:00.000Z"
        assert result["radar"]["host"] == "AMOY"
        assert result["radar"]["custom_field"] == "preserved"
        assert result["radar"]["status"] == "ready"

    def test_heartbeat_unknown_status_defaults_to_running(self, tmp_path: Path, monkeypatch):
        """If status field is missing, heartbeat writes 'running'."""
        patch_targets, manifest_path = _redirect_manifest(tmp_path, {
            "radar": {"pid": 1, "version": "v1.3.3"}
        })
        for target, value in patch_targets:
            monkeypatch.setattr(target, value)

        from panopticon_py.utils.process_guard import update_heartbeat

        update_heartbeat("radar")
        result = json.loads(manifest_path.read_text())
        assert result["radar"]["status"] == "running"
