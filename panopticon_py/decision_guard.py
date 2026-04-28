from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


GRAPHIFY_BLOCKED_SOURCE_MARKERS = (
    "graphify",
    "graph_report",
    "graph_json",
    "human_read_only",
)


@dataclass(frozen=True)
class SignalArtifactMeta:
    name: str
    source: str
    timestamp: str
    version: str


def assert_signal_artifacts_contract(
    artifacts: Iterable[SignalArtifactMeta],
    *,
    allowed_sources: set[str],
) -> None:
    for artifact in artifacts:
        source = (artifact.source or "").strip()
        if not source:
            raise ValueError(f"signal artifact {artifact.name!r} missing source")
        if source not in allowed_sources:
            raise ValueError(f"signal artifact {artifact.name!r} source {source!r} not in whitelist")
        if any(marker in source.lower() for marker in GRAPHIFY_BLOCKED_SOURCE_MARKERS):
            raise ValueError(
                f"signal artifact {artifact.name!r} source {source!r} is graphify-derived and blocked"
            )
        if not (artifact.timestamp or "").strip():
            raise ValueError(f"signal artifact {artifact.name!r} missing timestamp")
        if not (artifact.version or "").strip():
            raise ValueError(f"signal artifact {artifact.name!r} missing version")
