from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Literal, TypedDict
from uuid import uuid4

from panopticon_py.time_utils import utc_now_rfc3339_ms

Layer = Literal["L1", "L2", "L3", "L4", "L5"]


class L1Payload(TypedDict, total=False):
    delta_h: float
    ofi: float
    lambda_impact: float
    best_bid: float
    best_ask: float
    latency_ms: float


class L2Payload(TypedDict, total=False):
    trust_score: float
    signal_reliability: float
    external_event_score: float
    sentiment_score: float
    evidence_vector: dict[str, float]


class L3Payload(TypedDict, total=False):
    prior_probability: float
    likelihood_ratio: float
    posterior_probability: float
    ev_net: float
    kelly_fraction: float
    action: str


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    layer: Layer
    event_type: str
    event_ts: str
    ingest_ts_utc: str
    source: str
    version_tag: str
    payload: dict[str, Any]
    source_event_id: str | None = None
    market_id: str | None = None
    asset_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now_iso() -> str:
    return utc_now_rfc3339_ms()


def build_event(
    *,
    layer: Layer,
    event_type: str,
    source: str,
    version_tag: str,
    payload: dict[str, Any],
    source_event_id: str | None = None,
    market_id: str | None = None,
    asset_id: str | None = None,
) -> EventEnvelope:
    now = utc_now_iso()
    return EventEnvelope(
        event_id=str(uuid4()),
        layer=layer,
        event_type=event_type,
        event_ts=now,
        ingest_ts_utc=now,
        source=source,
        source_event_id=source_event_id,
        version_tag=version_tag,
        market_id=market_id,
        asset_id=asset_id,
        payload=payload,
    )
