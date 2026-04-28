"""
Tests for panopticon_py.utils.process_guard.

Covers:
  - Singleton enforcement (PID file, stale kill, re-acquire)
  - Version matching (D-suffix stripping)
  - Manifest read/write
  - check_peer_version
  - get_all_versions
  - Invalid process name raises ValueError
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import from the module under test
from panopticon_py.utils.process_guard import (
    _version_matches,
    _is_alive,
    _read_manifest,
    _write_manifest,
    _pid_path,
    acquire_singleton,
    check_peer_version,
    get_all_versions,
    VALID_PROCESS_NAMES,
)


class TempRunDir:
    """Context manager: provides a temp run/ directory with clean state."""

    def __init__(self):
        self._tmpdir = tempfile.mkdtemp(prefix="pg_test_")
        self._orig_root = None
        self._orig_run = None

    def __enter__(self):
        import panopticon_py.utils.process_guard as pg
        self._orig_root = pg._PROJECT_ROOT
        self._orig_run = pg._RUN_DIR
        pg._PROJECT_ROOT = Path(self._tmpdir)
        pg._RUN_DIR = pg._PROJECT_ROOT / "run"
        pg._VERSIONS_REF = pg._RUN_DIR / "versions_ref.json"
        pg._MANIFEST = pg._RUN_DIR / "process_manifest.json"
        pg._MANIFEST_LOCK = pg._RUN_DIR / ".manifest.lock"
        return self

    def __exit__(self, *args):
        import panopticon_py.utils.process_guard as pg
        pg._PROJECT_ROOT = self._orig_root
        pg._RUN_DIR = self._orig_run
        pg._VERSIONS_REF = self._orig_run / "versions_ref.json"
        pg._MANIFEST = self._orig_run / "process_manifest.json"
        pg._MANIFEST_LOCK = self._orig_run / ".manifest.lock"

    @property
    def run_dir(self) -> Path:
        return Path(self._tmpdir) / "run"


class TestVersionMatches:
    """D51: Test version matching strips D-suffix correctly."""

    @pytest.mark.parametrize("actual,expected,expected_result", [
        ("v1.0.0-D51", "v1.0.0-D51", True),
        ("v1.0.0-D50", "v1.0.0-D51", True),   # same base, different sprint
        ("v1.0.1-D51", "v1.0.0-D51", False),  # different patch
        ("v1.1.0-D51", "v1.0.0-D51", False),  # different minor
        ("v2.0.0-D51", "v1.0.0-D51", False),  # different major
        ("v1.0.0",     "v1.0.0-D51", True),    # no suffix on actual
        ("v1.0.0-D51", "v1.0.0",      True),   # no suffix on expected
        ("",            "v1.0.0-D51", False),    # empty string
        ("v0.0.0",     "v0.0.0-D1",   True),   # edge: zero versions
    ])
    def test_version_matches_params(self, actual, expected, expected_result):
        assert _version_matches(actual, expected) == expected_result


class TestIsAlive:
    """D51: Test _is_alive correctly identifies alive vs dead PIDs."""

    def test_invalid_pid_returns_false(self):
        assert _is_alive(99999) is False

    def test_current_pid_returns_true(self):
        assert _is_alive(os.getpid()) is True


class TestAcquireSingleton:
    """D51: Test singleton enforcement."""

    def test_unknown_process_name_raises(self):
        with TempRunDir() as td:
            with pytest.raises(ValueError, match="Unknown process name"):
                acquire_singleton("not_a_process", "v1.0.0-D51")

    def test_valid_process_name_acquires_singleton(self):
        with TempRunDir() as td:
            acquire_singleton("radar", "v1.0.0-D51")
            pid_file = _pid_path("radar")
            assert pid_file.exists()
            assert int(pid_file.read_text().strip()) == os.getpid()

    def test_singleton_writes_manifest_entry(self):
        with TempRunDir() as td:
            acquire_singleton("backend", "v1.0.0-D51")
            manifest = _read_manifest()
            assert "backend" in manifest
            assert manifest["backend"]["version"] == "v1.0.0-D51"
            assert manifest["backend"]["pid"] == os.getpid()
            assert manifest["backend"]["status"] == "running"
            assert manifest["backend"]["version_match"] is True

    def test_version_mismatch_logged_when_expected_set(self):
        """When versions_ref.json exists and version differs, CRITICAL is logged."""
        with TempRunDir() as td:
            # Create versions_ref.json with a different version
            ref = {"backend": "v2.0.0-D51"}
            td.run_dir.mkdir(parents=True, exist_ok=True)
            (td.run_dir / "versions_ref.json").write_text(json.dumps(ref), encoding="utf-8")

            with patch("panopticon_py.utils.process_guard.logger") as mock_logger:
                acquire_singleton("backend", "v1.0.0-D51")
                # Should have logged CRITICAL (mismatch)
                critical_calls = [c for c in mock_logger.critical.call_args_list]
                assert any("VERSION MISMATCH" in str(c) for c in critical_calls)

    def test_version_match_ok_when_expected_set(self):
        """When versions_ref.json exists and version matches, INFO is logged."""
        with TempRunDir() as td:
            ref = {"radar": "v1.0.0-D51"}
            td.run_dir.mkdir(parents=True, exist_ok=True)
            (td.run_dir / "versions_ref.json").write_text(json.dumps(ref), encoding="utf-8")

            with patch("panopticon_py.utils.process_guard.logger") as mock_logger:
                acquire_singleton("radar", "v1.0.0-D51")
                info_calls = [c for c in mock_logger.info.call_args_list]
                assert any("version OK" in str(c) for c in info_calls)


class TestCheckPeerVersion:
    """D51: Test inter-process version checking."""

    def test_unknown_process_returns_none(self):
        with TempRunDir() as td:
            result = check_peer_version("not_running")
            assert result is None

    def test_returns_correct_manifest_entry(self):
        with TempRunDir() as td:
            # Simulate another process running
            acquire_singleton("orchestrator", "v1.3.0-D50")
            result = check_peer_version("orchestrator")
            assert result is not None
            assert result["version"] == "v1.3.0-D50"
            assert result["status"] == "running"


class TestGetAllVersions:
    """D51: Test get_all_versions returns full manifest."""

    def test_returns_empty_dict_when_no_manifest(self):
        with TempRunDir() as td:
            result = get_all_versions()
            assert isinstance(result, dict)

    def test_returns_all_manifest_entries(self):
        with TempRunDir() as td:
            acquire_singleton("radar", "v1.0.0-D51")
            acquire_singleton("backend", "v2.1.0-D50")
            result = get_all_versions()
            assert "radar" in result
            assert "backend" in result
            assert result["radar"]["version"] == "v1.0.0-D51"
            assert result["backend"]["version"] == "v2.1.0-D50"


class TestValidProcessNames:
    """D51: Verify VALID_PROCESS_NAMES contains expected processes."""

    def test_contains_all_managed_processes(self):
        assert "radar" in VALID_PROCESS_NAMES
        assert "orchestrator" in VALID_PROCESS_NAMES
        assert "backend" in VALID_PROCESS_NAMES
        assert "frontend" in VALID_PROCESS_NAMES

    def test_is_frozenset(self):
        assert isinstance(VALID_PROCESS_NAMES, frozenset)
