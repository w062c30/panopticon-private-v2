"""
panopticon_py.utils — Process singleton enforcement and version identity.

Public API:
    acquire_singleton(name, version)  — enforce singleton + register version
    check_peer_version(name, required_base)  — read peer's version from manifest
    get_all_versions()  — return full process_manifest.json
"""

from panopticon_py.utils.process_guard import (
    acquire_singleton,
    check_peer_version,
    get_all_versions,
)

__all__ = ["acquire_singleton", "check_peer_version", "get_all_versions"]
